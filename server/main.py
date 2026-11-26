from __future__ import annotations

import asyncio
import copy
import json
import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import auth, catalog, db, engine, gates, geo, limits, models, worldsim
from .config import settings
from .dto import (anchor_view, character_view, journal_view, knowledge_view,
                  presentation_view, public_user, refuge_view)
from .locks import LockBusy, campaign_lock
from .packs import invalidate, list_packs, load_pack, validate_pack
from .realtime import manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await auth.bootstrap_operator()
    invalidate()
    geo.invalidate_basemap()
    models.invalidate()
    for pack_id in list_packs():
        errors = validate_pack(pack_id)
        if errors:
            print(f"[PACK] {pack_id}: {errors}")
    await engine.recover_stale_resolutions()

    async def _recovery_loop() -> None:
        ticks = 0
        while True:
            await asyncio.sleep(60)
            ticks += 1
            try:
                await engine.recover_stale_resolutions()
                await engine.expire_orders()
                if ticks % 60 == 0:  # once an hour
                    await db.execute(
                        "DELETE FROM llm_logs WHERE created_at < datetime('now','-30 days')"
                    )
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception("recovery tick failed")

    recovery_task = asyncio.create_task(_recovery_loop())
    try:
        yield
    finally:
        recovery_task.cancel()


app = FastAPI(title="Text World", version="0.0.29", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.public_base.rstrip('/'),
        "https://odisodisseyt-design.github.io",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


def _conflict(error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": error, "message": message})


_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _schedule_resolution(campaign_id: str, step_no: int) -> None:
    """Resolve the step outside the player's request.

    The literary model is the slowest part of a turn and may take tens of
    seconds. The order is already committed, so the HTTP response must not
    wait for prose: the client follows /step/status and the event feed.
    """
    async def run() -> None:
        try:
            async with asyncio.timeout(150):
                await engine._resolve_step(campaign_id, expected_step_no=step_no or None)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("background resolution failed")

    task = asyncio.create_task(run())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _busy_response(retry_after: int, error: str = "STEP_BUSY",
                   message: str = "Мир ещё обрабатывает шаг. Повтори чуть позже.") -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": error, "message": message, "retry_after": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


@app.exception_handler(LockBusy)
async def lock_busy(request, exc: LockBusy):
    return _busy_response(exc.retry_after)


@app.exception_handler(limits.RateLimited)
async def rate_limited(request, exc: limits.RateLimited):
    return JSONResponse(
        status_code=429,
        content={"error": "RATE_LIMITED", "message": "Слишком много приказов. Подожди немного.",
                 "retry_after": exc.retry_after},
        headers={"Retry-After": str(exc.retry_after)},
    )


@app.exception_handler(limits.CampaignLimit)
async def campaign_limit(request, exc: limits.CampaignLimit):
    return JSONResponse(
        status_code=409,
        content={"error": "CAMPAIGN_LIMIT",
                 "message": "У тебя уже 10 игр. Удали одну, чтобы начать новую."},
    )


@app.exception_handler(Exception)
async def unhandled(request, exc):
    await db.execute(
        "INSERT INTO audit_logs(user_id,action,payload) VALUES(NULL,'server_error',?)",
        (db.dumps({"path": request.url.path, "error": traceback.format_exc()[-2000:]}),),
    )
    return JSONResponse(
        status_code=500,
        content={"error": "INTERNAL", "message": "Мир не ответил. Попробуй ещё раз."},
    )


async def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Требуется вход.")
    token = authorization.split(" ", 1)[1].strip()
    user = await auth.user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Сессия истекла.")
    return user


def _admin(user: dict[str, Any]) -> None:
    if user["role"] != "operator":
        raise HTTPException(status_code=403, detail="Недостаточно прав.")


def _shop_items(pack: dict[str, Any], state: dict[str, Any],
                member: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    ctx = gates.context(pack, state, member)
    stock = state.setdefault("shop_stock", {})
    items: list[dict[str, Any]] = []
    for item in pack["shop"].get("items", []):
        if not gates.check(item.get("requires"), ctx):
            continue
        iid = item.get("id", "")
        if iid not in stock:
            stock[iid] = int(item.get("stock", 0) or 0)
        price = item.get("price", {}) or {}
        currency = next(iter(price), "")
        amount = next(iter(price.values()), 0)
        data = catalog.item(pack, iid) or {}
        items.append({
            "id": iid, "name": item.get("name") or data.get("name", iid),
            "description": item.get("desc") or data.get("desc", ""),
            "type": _SECTION_NAMES.get(str(item.get("section", "")), "Прочее"),
            "slot": _SECTION_NAMES.get(str(item.get("section", "")), "Прочее"),
            "price": int(amount or 0), "currency": currency,
            "stock": int(stock.get(iid, 0) or 0), "quantity": 1, "rarity": "обычный",
        })
    return items


def _quests(pack: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    flags = gates.context(pack, state, (state.get("members") or [None])[0])["flags"]
    knowledge = set(state.get("knowledge", []))
    result: list[dict[str, Any]] = []
    for quest in pack.get("quests", {}).get("quests", []):
        if not gates.check(quest.get("requires"), gates.context(pack, state, (state.get("members") or [None])[0])):
            continue
        objectives = []
        for obj in quest.get("objectives", []):
            if isinstance(obj, dict):
                text = obj.get("text", "")
                done = (obj.get("flag") in flags) if obj.get("flag") else (
                    obj.get("knowledge") in knowledge if obj.get("knowledge") else False)
            else:
                text, done = str(obj), False
            objectives.append({"text": text, "done": bool(done)})
        result.append({
            "id": quest.get("id", ""), "name": quest.get("name", ""),
            "description": quest.get("desc", ""), "kind": quest.get("type", "main"),
            "objectives": objectives, "time_left": "", "rewards": [],
            "complete": bool(objectives) and all(o["done"] for o in objectives),
        })
    return result


_SECTION_NAMES = {
    "consumables": "Расходные", "equipment": "Снаряжение",
    "materials": "Материалы", "structures": "Для убежища",
}


def _map_nodes(state: dict[str, Any], member: dict[str, Any]) -> list[dict[str, Any]]:
    """Only discovered places and their direct roads, mapped onto real Moscow.

    The client must never receive the full location table: an unvisited place
    stays hidden until a visited neighbour opens a road to it.
    """
    locations = state["world"].get("locations", [])
    by_id = {str(item.get("id", "")): item for item in locations}
    current_id = str((member.get("character") or {}).get("location") or "")
    visited = {str(item.get("id", "")) for item in locations if item.get("visited")}
    if current_id:
        visited.add(current_id)

    visible = set(visited)
    for location_id in list(visited):
        for edge in (by_id.get(location_id) or {}).get("exits", []) or []:
            target = str(edge.get("to") or "")
            if target in by_id:
                visible.add(target)

    nodes: list[dict[str, Any]] = []
    for index, node in enumerate(locations):
        location_id = str(node.get("id", ""))
        if location_id not in visible:
            continue
        view = geo.location_view(node, index)
        coords = node.get("coords") or []
        view.update({
            "visited": location_id in visited,
            "current": location_id == current_id,
            "floor": int(coords[2]) if len(coords) > 2 else int(node.get("floor", 0) or 0),
            "connections": [
                str(edge.get("to")) for edge in (node.get("exits") or [])
                if str(edge.get("to") or "") in visible
            ],
        })
        nodes.append(view)
    return nodes


def _npcs(pack: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    locations = {loc.get("id"): loc.get("name", "") for loc in state["world"].get("locations", [])}
    result: list[dict[str, Any]] = []
    for npc_id, npc in state.get("npcs", {}).items():
        if not npc.get("alive", True):
            continue
        result.append({
            "id": npc_id, "name": npc.get("name", ""),
            "description": npc.get("role", ""), "goals": npc.get("goals", []),
            "known": npc.get("memory", []), "relations": {},
            "conditions": npc.get("needs", {}), "alive": True,
            "emotion": npc.get("disposition", "нейтрален"), "portrait": None,
            "location": locations.get(npc.get("location"), npc.get("location", "")),
            "activity": npc.get("activity", ""),
        })
    return result


async def _campaign_state(campaign: dict[str, Any], state: dict[str, Any],
                          member: dict[str, Any]) -> dict[str, Any]:
    pack = load_pack(campaign["pack_id"])
    ui = dict(pack["ui_manifest"])
    ui.setdefault("pack_id", campaign["pack_id"])
    ui.setdefault("title", pack["manifest"].get("name", ""))
    journal = await db.fetchall(
        """SELECT id,text,status,step_no,source FROM ledger
           WHERE campaign_id=? ORDER BY id DESC LIMIT 200""",
        (campaign["id"],),
    )
    pending_ids = db.loads(campaign["pending_json"], [])
    waiting_names = await engine._user_names(pending_ids)
    living = engine._living_view(pack, state)
    character = character_view(member)
    # Items are clickable in the client, so each stack carries its own catalog
    # description instead of a bare id/name pair.
    enriched = []
    for entry in character.get("inventory", []):
        view = catalog.item_view(pack, entry.get("id"), int(entry.get("quantity", 1) or 1))
        data = catalog.item(pack, entry.get("id")) or {}
        view["weight_kg"] = float(entry.get("weight_kg", 0) or 0)
        view["weight"] = round(view["weight_kg"] * int(view.get("quantity", 1) or 1), 2)
        view["equipped"] = entry.get("id") in (character.get("equipment") or {}).values()
        view["usable"] = bool(data.get("effect") or data.get("effects"))
        view["equippable"] = _equipment_slot(data)
        enriched.append(view)
    character["inventory"] = enriched
    return {
        "id": campaign["id"], "name": campaign["name"], "pack_id": campaign["pack_id"],
        "description": str(pack["manifest"].get("description", "")),
        "intro": str(pack["manifest"].get("intro", "")) if int(campaign["step_no"]) <= 1 else "",
        "step_no": campaign["step_no"], "phase": campaign["phase"],
        "waiting_for": waiting_names,
        "my_turn": member["user_id"] in pending_ids,
        "ui_manifest": ui, "anchor": anchor_view(state, member),
        "character": character, "presentation": presentation_view(state),
        "lore": pack["lore"][:8000], "quests": _quests(pack, state),
        "map_nodes": _map_nodes(state, member), "npcs": _npcs(pack, state),
        "journal": journal_view(journal), "shop_items": _shop_items(pack, state, member),
        "era": state.get("era", "E0"),
        "era_name": living.get("era_name", ""),
        "class_selection": engine._class_selection_view(pack, state, member),
        "living": living,
        "refuge": refuge_view(state.get("refuge", {})),
        "knowledge": knowledge_view(state.get("knowledge", [])),
        "pack_version": campaign.get("pack_version") or load_pack(campaign["pack_id"])["manifest"].get("version", ""),
        "language": campaign.get("language") or "ru",
        "model": models.get(campaign.get("model_id")).view(),
    }


# ---------------- auth ----------------

async def _ensure_welcome_game(user: dict[str, Any]) -> None:
    """Every account gets a playable world on first entry, so the list is never empty."""
    rows = await db.fetchall(
        """SELECT c.state_json FROM campaigns c JOIN campaign_members m ON m.campaign_id=c.id
           WHERE m.user_id=? AND m.status='active'""",
        (user["id"],),
    )
    for row in rows:
        state = db.loads(row["state_json"], {})
        if int(state.get("state_version", 0) or 0) >= 2:
            return
    try:
        await engine.create_campaign("refuge", "Первый час", user, [], character_name="Странник")
    except limits.CampaignLimit:
        return


@app.post("/api/auth/register")
async def register(payload: dict[str, Any]):
    user = await auth.register(str(payload.get("username", "")), str(payload.get("password", "")))
    if not user:
        return {"error": "INVALID", "message": "Имя занято или слишком короткие данные."}
    token = await auth.login(user["username"], str(payload["password"]))
    await _ensure_welcome_game(user)
    return {"user": public_user(user), "token": token}


@app.post("/api/auth/login")
async def login(payload: dict[str, Any]):
    token = await auth.login(str(payload.get("username", "")), str(payload.get("password", "")))
    if not token:
        return {"error": "INVALID", "message": "Неверный логин или пароль."}
    user = await auth.user_from_token(token)
    await _ensure_welcome_game(user)
    return {"token": token, "user": public_user(user)}


@app.post("/api/auth/logout")
async def logout(authorization: str | None = Header(default=None), user=Depends(current_user)):
    if authorization:
        await auth.logout(authorization.split(" ", 1)[1].strip())
    return {"ok": True}


@app.get("/api/me")
async def me(user=Depends(current_user)):
    return public_user(user)


# ---------------- system ----------------

@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "version": app.version,
        "min_client_version": settings.min_client_version,
        "public_base": settings.public_base,
        "packs": list_packs(),
        "provider": settings.provider,
    }


@app.get("/api/packs")
async def packs(user=Depends(current_user)):
    result = []
    for pack_id in list_packs():
        pack = load_pack(pack_id)
        result.append({"id": pack_id, "manifest": pack["manifest"], "ui_manifest": pack["ui_manifest"]})
    return {"packs": result}


@app.get("/api/pack/{pack_id}/ui-manifest")
async def ui_manifest(pack_id: str, user=Depends(current_user)):
    return load_pack(pack_id)["ui_manifest"]


@app.get("/api/map/{pack_id}")
async def pack_map(pack_id: str, user=Depends(current_user)):
    """Real Moscow basemap for a pack: roads, river, rail and landmark labels."""
    if pack_id not in list_packs():
        raise HTTPException(status_code=404, detail="Карта недоступна.")
    data = geo.load_basemap(pack_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Карта недоступна.")
    return JSONResponse(data, headers={"Cache-Control": "private, max-age=86400"})


# ---------------- campaigns ----------------

@app.post("/api/campaign/new")
async def new_campaign(payload: dict[str, Any], user=Depends(current_user)):
    pack_id = str(payload.get("pack_id", settings.default_pack))
    invitees = []
    for uid in payload.get("invitees", []):
        invited = await db.fetchone("SELECT * FROM users WHERE id = ? AND id != ?", (uid, user["id"]))
        if invited:
            invitees.append(invited)
    model_id = str(payload.get("model_id", settings.default_model_id))
    if not models.validate(model_id):
        raise HTTPException(status_code=422, detail="Такая модель недоступна.")
    campaign_id = await engine.create_campaign(
        pack_id, str(payload.get("name") or "Первый час"), user, invitees, model_id,
        character_name=str(payload.get("character_name") or ""),
    )
    return {"campaign_id": campaign_id}


@app.get("/api/models")
async def list_models(user=Depends(current_user)):
    return {"models": models.visible()}


@app.post("/api/campaign/delete")
async def remove_campaign(payload: dict[str, Any], user=Depends(current_user)):
    return await engine.delete_campaign(str(payload.get("campaign_id", "")), user["id"])


@app.post("/api/campaign/{campaign_id}/model")
async def change_model(campaign_id: str, payload: dict[str, Any], user=Depends(current_user)):
    result = await engine.set_campaign_model(campaign_id, user["id"], str(payload.get("model_id", "")))
    if result.get("error") == "NOT_FOUNDER":
        raise HTTPException(status_code=403, detail=result["message"])
    if result.get("error") in ("STEP_IN_PROGRESS", "MODEL_UNAVAILABLE"):
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@app.get("/api/campaign/{campaign_id}")
async def get_campaign(campaign_id: str, user=Depends(current_user)):
    ctx = await engine.get_context(campaign_id, user["id"])
    if not ctx:
        raise HTTPException(status_code=404, detail="Игра не найдена.")
    campaign, state, member, membership = ctx
    return await _campaign_state(campaign, state, member)


@app.get("/api/updates/manifest")
async def updates_manifest(request: Request):
    """Self-hosted Expo Updates v1 manifest (OTA code/assets, not the APK)."""
    if request.headers.get("expo-protocol-version") != "1":
        return Response(status_code=406)
    platform = request.headers.get("expo-platform")
    if platform not in ("android", "ios"):
        return Response(status_code=400)
    runtime = request.headers.get("expo-runtime-version", "")
    path = settings.updates_dir / "manifest.json"
    if not path.exists():
        return Response(status_code=204)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Response(status_code=204)
    if str(manifest.get("runtimeVersion", "")) != runtime:
        return Response(status_code=204)
    headers = {
        "expo-protocol-version": "1",
        "expo-sfv-version": "0",
        "expo-manifest-filters": "{}",
        "expo-server-defined-headers": "{}",
        "cache-control": "private, max-age=0",
    }
    return Response(
        content=db.dumps(manifest),
        media_type="application/expo+json",
        headers=headers,
    )


@app.get("/api/updates/assets/{name}")
async def updates_asset(name: str):
    """Immutable asset referenced by the OTA manifest."""
    if "/" in name or ".." in name or "\\" in name:
        raise HTTPException(status_code=404, detail="Not found")
    path = settings.updates_dir / "assets" / name
    root = settings.updates_dir.resolve()
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    ext = path.suffix.lower()
    media = {
        ".hbc": "application/javascript", ".js": "application/javascript",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".ttf": "font/ttf", ".otf": "font/otf",
    }.get(ext, "application/octet-stream")
    return Response(
        content=path.read_bytes(), media_type=media,
        headers={"cache-control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/campaign/{campaign_id}/step/status")
async def step_status(campaign_id: str, user=Depends(current_user)):
    ctx = await engine.get_context(campaign_id, user["id"])
    if not ctx:
        raise HTTPException(status_code=404, detail="Игра не найдена.")
    campaign, state, member, _ = ctx
    pending = db.loads(campaign["pending_json"], [])
    active = [m["user_id"] for m in state.get("members", [])
              if m.get("status", "active") == "active"]
    submitted = [uid for uid in active if uid not in pending]
    participants: list[dict[str, Any]] = []
    for uid, is_submitted in [(uid, True) for uid in submitted] + [(uid, False) for uid in pending]:
        participants.append({
            "name": (await engine._user_names([uid]))[0],
            "submitted": is_submitted,
            "isYou": uid == user["id"],
        })
    row = await db.fetchone(
        "SELECT created_at FROM steps WHERE campaign_id=? AND step_no=?",
        (campaign_id, int(campaign["step_no"]) - 1),
    )
    return {
        "campaignId": campaign_id,
        "step": campaign["step_no"],
        "phase": campaign["phase"],
        "deadline": campaign.get("order_deadline_ts"),
        "participants": participants,
        "resolved_at": row["created_at"] if row else None,
        "results_ready": row is not None,
        "my_turn": user["id"] in pending,
    }


@app.get("/api/campaign/{campaign_id}/steps")
async def steps_history(campaign_id: str, limit: int = 20, cursor: int = 0,
                        user=Depends(current_user)):
    ctx = await engine.get_context(campaign_id, user["id"])
    if not ctx:
        raise HTTPException(status_code=404, detail="Игра не найдена.")
    rows = await db.fetchall(
        """SELECT step_no,result_json,created_at FROM steps
           WHERE campaign_id=? AND step_no>? ORDER BY step_no DESC LIMIT ?""",
        (campaign_id, cursor, max(1, min(limit, 50))),
    )
    result = []
    for row in rows:
        payload = db.loads(row["result_json"], {})
        narrations = payload.get("narrations") or {}
        own_text = " ; ".join(
            item.get("text", "") for item in payload.get("results", [])
            if item.get("user_id") == user["id"]
        )
        result.append({
            "step_no": row["step_no"],
            "prose": str(narrations.get(str(user["id"]), "")),
            "outcome_text": own_text,
            "created_at": row["created_at"],
        })
    return {"steps": result}


@app.get("/api/campaign/{campaign_id}/events")
async def campaign_events(campaign_id: str, after: int = 0, limit: int = 100,
                          user=Depends(current_user)):
    ctx = await engine.get_context(campaign_id, user["id"])
    if not ctx:
        raise HTTPException(status_code=404, detail="Игра не найдена.")
    rows = await db.fetchall(
        """SELECT seq,event,payload_json FROM campaign_events
           WHERE campaign_id=? AND seq>? AND (user_id IS NULL OR user_id=?)
           ORDER BY seq LIMIT ?""",
        (campaign_id, after, user["id"], max(1, min(limit, 200))),
    )
    return {
        "events": [
            {"seq": row["seq"], "event": row["event"],
             "payload": db.loads(row["payload_json"], {})}
            for row in rows
        ]
    }


@app.get("/api/campaigns/mine")
async def my_campaigns(user=Depends(current_user)):
    rows = await db.fetchall(
        """SELECT c.* FROM campaigns c JOIN campaign_members m ON m.campaign_id = c.id
           WHERE m.user_id = ? AND m.status = 'active' ORDER BY c.updated_at DESC""",
        (user["id"],),
    )
    result: list[dict[str, Any]] = []
    for campaign in rows:
        ctx = await engine.get_context(campaign["id"], user["id"])
        if ctx:
            result.append(await _campaign_state(ctx[0], ctx[1], ctx[2]))
    return {"campaigns": result}


@app.post("/api/campaign/leave")
async def leave_campaign(payload: dict[str, Any], user=Depends(current_user)):
    campaign_id = str(payload.get("campaign_id", ""))
    name = (await engine._user_names([user["id"]]))[0]
    async with engine.mutate(campaign_id, user["id"]) as m:
        if m is None or m.member is None:
            return {"error": "NOT_IN_CAMPAIGN"}
        await m.conn.execute(
            """UPDATE campaign_members SET status='left',left_at=datetime('now')
               WHERE campaign_id=? AND user_id=?""",
            (campaign_id, user["id"]),
        )
        for entry in m.state.get("members", []):
            if entry["user_id"] == user["id"]:
                entry["status"] = "left"
        m.pending = [p for p in m.pending if p != user["id"]]
    await engine.emit(campaign_id, None, "member_left", {
        "name": name, "pending": await engine._user_names(m.pending),
    })
    return {"ok": True, "campaign_id": campaign_id}


@app.post("/api/turn")
async def turn(
    payload: dict[str, Any],
    user=Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    campaign_id = str(payload.get("campaign_id", ""))
    clarification_id = str(payload.get("clarification_id") or "").strip()
    # The new client sends "command"; older clients send "text".
    text = str(payload.get("text") or payload.get("command") or "").strip()
    if not text:
        if clarification_id:
            result = await engine.cancel_clarification(campaign_id, user["id"], clarification_id)
            if result.get("error"):
                return _conflict("CLARIFICATION_EXPIRED", result["message"])
            return result
        return {"error": "EMPTY", "message": "Напиши, что делаешь."}
    if len(text) > settings.turn_max_chars:
        return JSONResponse(
            status_code=422,
            content={"error": "TEXT_TOO_LONG",
                     "message": f"Приказ длиннее {settings.turn_max_chars} символов."},
        )
    if idempotency_key and len(idempotency_key) > 128:
        return JSONResponse(
            status_code=400,
            content={"error": "BAD_IDEMPOTENCY_KEY", "message": "Ключ приказа слишком длинный."},
        )
    if settings.require_idempotency and not idempotency_key:
        return JSONResponse(
            status_code=428,
            content={"error": "IDEMPOTENCY_REQUIRED", "message": "Обнови приложение."},
        )
    if idempotency_key:
        status, cached = await engine._load_idempotent(idempotency_key, campaign_id, user["id"])
        if status == "ok":
            return cached
        if status == "mismatch":
            return _conflict("IDEMPOTENCY_MISMATCH", "Ключ приказа уже использован.")

    await limits.check_turn_rate(user["id"])
    result = await engine.submit_order(campaign_id, user["id"], text, idempotency_key,
                                       defer_resolution=True,
                                       clarification_id=clarification_id or None)
    if result.get("error") == "CLARIFICATION_EXPIRED":
        return _conflict("CLARIFICATION_EXPIRED", result["message"])
    if result.get("error") == "IDEMPOTENCY_MISMATCH":
        return _conflict("IDEMPOTENCY_MISMATCH", "Ключ приказа уже использован.")
    if result.get("error") == "TEXT_TOO_LONG":
        return JSONResponse(status_code=422, content={"error": "TEXT_TOO_LONG",
                                                      "message": "Приказ слишком длинный."})
    if result.pop("needs_resolution", False):
        _schedule_resolution(campaign_id, int(result.get("step_no") or 0))
    return result


@app.post("/api/allocate")
async def allocate(payload: dict[str, Any], user=Depends(current_user)):
    campaign_id = str(payload.get("campaign_id", ""))
    async with engine.mutate(campaign_id, user["id"]) as m:
        if m is None or m.member is None:
            raise HTTPException(status_code=404, detail="Игра не найдена.")
        character = m.member["character"]
        points = int(character.get("unspent_points", 0))
        for item in payload.get("allocations", []):
            key = str(item.get("id", ""))
            delta = int(item.get("delta", 0))
            if key in character.get("attributes", {}) and delta > 0 and points >= delta:
                character["attributes"][key] += delta
                points -= delta
        character["unspent_points"] = points
        engine._normalize_character(m.pack, character)
        view = character_view(m.member)
    await engine.emit(campaign_id, user["id"], "character_updated", {"character": view})
    return {"character": view}


@app.get("/api/shop/{campaign_id}")
async def get_shop(campaign_id: str, user=Depends(current_user)):
    ctx = await engine.get_context(campaign_id, user["id"])
    if not ctx:
        raise HTTPException(status_code=404, detail="Игра не найдена.")
    campaign, state, member, _ = ctx
    pack = load_pack(campaign["pack_id"])
    shop = pack["shop"]
    items = _shop_items(pack, state, member)
    return {
        "enabled": shop.get("enabled", False),
        "currencies": shop.get("currencies", []),
        "sections": shop.get("sections", []),
        "items": items,
        "wallet": member["character"].get("currency", {}),
    }


@app.post("/api/shop/buy")
async def buy(payload: dict[str, Any], user=Depends(current_user)):
    campaign_id = str(payload.get("campaign_id", ""))
    item_id = str(payload.get("item_id", ""))
    async with engine.mutate(campaign_id, user["id"]) as m:
        if m is None or m.member is None:
            raise HTTPException(status_code=404, detail="Игра не найдена.")
        item = next((i for i in m.pack["shop"].get("items", []) if i["id"] == item_id), None)
        if not item:
            return {"error": "NOT_FOUND", "message": "Такого товара нет."}
        if not gates.check(item.get("requires"), gates.context(m.pack, m.state, m.member)):
            return {"error": "NOT_AVAILABLE", "message": "Этот товар ещё не появился."}
        stock = m.state.setdefault("shop_stock", {})
        if item_id not in stock:
            stock[item_id] = int(item.get("stock", 0) or 0)
        if stock.get(item_id, 0) <= 0:
            return {"error": "OUT_OF_STOCK", "message": "Товара нет в наличии."}
        wallet = m.member["character"].setdefault("currency", {})
        for currency, amount in item.get("price", {}).items():
            if wallet.get(currency, 0) < amount:
                return {"error": "INSUFFICIENT_RESOURCE", "message": "Не хватает монеток."}
        for currency, amount in item.get("price", {}).items():
            wallet[currency] -= amount
        stock[item_id] -= 1
        m.state["id"] = campaign_id
        engine._apply_effects(m.pack, m.state, m.member, item.get("effect", {}), None,
                              m.pack["rules"], f"buy:{item_id}")
        view = character_view(m.member)
        stock_left = stock[item_id]
    await engine.emit(campaign_id, user["id"], "character_updated", {"character": view})
    return {"ok": True, "character": view, "stock_left": stock_left}


def _equipment_slot(item: dict[str, Any]) -> str:
    kind = str(item.get("kind", "")).lower()
    if kind in ("weapon", "melee", "ranged", "blade", "blunt"):
        return "weapon"
    if kind in ("armor", "clothing", "gear"):
        return "armor"
    return ""


@app.post("/api/inventory/use")
async def use_item(payload: dict[str, Any], user=Depends(current_user)):
    campaign_id = str(payload.get("campaign_id", ""))
    item_id = str(payload.get("item_id", ""))
    async with engine.mutate(campaign_id, user["id"]) as m:
        if m is None or m.member is None:
            raise HTTPException(status_code=404, detail="Игра не найдена.")
        item = catalog.item(m.pack, item_id)
        if not item:
            return {"error": "NOT_FOUND", "message": "Такого предмета нет."}
        inventory = m.member["character"].get("inventory", [])
        entry = next((x for x in inventory if x.get("id") == item_id), None)
        if not entry or int(entry.get("quantity", 0) or 0) < 1:
            return {"error": "NOT_FOUND", "message": "Этого предмета нет в сумке."}
        effects = item.get("effect") or item.get("effects") or {}
        if not effects:
            return {"error": "NOT_USABLE",
                    "message": f"«{item.get('name', 'Предмет')}» сейчас не нужен."}
        engine._remove_item(m.member["character"], item_id, 1, m.pack)
        m.state["id"] = campaign_id
        engine._apply_effects(m.pack, m.state, m.member, effects, None,
                              m.pack["rules"], f"use:{item_id}")
        view = character_view(m.member)
    await engine.emit(campaign_id, user["id"], "character_updated", {"character": view})
    return {"ok": True, "character": view,
            "message": f"Ты используешь «{item.get('name', 'предмет')}»."}


@app.post("/api/inventory/equip")
async def equip_item(payload: dict[str, Any], user=Depends(current_user)):
    campaign_id = str(payload.get("campaign_id", ""))
    item_id = str(payload.get("item_id", ""))
    async with engine.mutate(campaign_id, user["id"]) as m:
        if m is None or m.member is None:
            raise HTTPException(status_code=404, detail="Игра не найдена.")
        item = catalog.item(m.pack, item_id)
        if not item:
            return {"error": "NOT_FOUND", "message": "Такого предмета нет."}
        slot = _equipment_slot(item)
        if not slot:
            return {"error": "NOT_EQUIPPABLE", "message": "Это нельзя надеть."}
        inventory = m.member["character"].get("inventory", [])
        if not any(x.get("id") == item_id for x in inventory):
            return {"error": "NOT_FOUND", "message": "Этого предмета нет в сумке."}
        m.member["character"].setdefault("equipment", {})[slot] = item_id
        engine._normalize_character(m.pack, m.member["character"])
        view = character_view(m.member)
    await engine.emit(campaign_id, user["id"], "character_updated", {"character": view})
    return {"ok": True, "character": view,
            "message": f"Ты берёшь «{item.get('name', 'предмет')}»."}


@app.post("/api/inventory/unequip")
async def unequip_item(payload: dict[str, Any], user=Depends(current_user)):
    campaign_id = str(payload.get("campaign_id", ""))
    slot = str(payload.get("slot", ""))
    async with engine.mutate(campaign_id, user["id"]) as m:
        if m is None or m.member is None:
            raise HTTPException(status_code=404, detail="Игра не найдена.")
        equipment = m.member["character"].setdefault("equipment", {})
        if slot not in equipment:
            return {"error": "NOT_FOUND", "message": "Эта ячейка пуста."}
        equipment.pop(slot, None)
        engine._normalize_character(m.pack, m.member["character"])
        view = character_view(m.member)
    await engine.emit(campaign_id, user["id"], "character_updated", {"character": view})
    return {"ok": True, "character": view, "message": "Ты убираешь предмет."}


@app.get("/api/quests/{campaign_id}")
async def get_quests(campaign_id: str, user=Depends(current_user)):
    ctx = await engine.get_context(campaign_id, user["id"])
    if not ctx:
        raise HTTPException(status_code=404, detail="Игра не найдена.")
    campaign, state, member, _ = ctx
    pack = load_pack(campaign["pack_id"])
    return {"quests": _quests(pack, state)}


@app.post("/api/shop/{action}")
async def shop_action(action: str, payload: dict[str, Any], user=Depends(current_user)):
    if action == "buy":
        return await buy(payload, user)
    if action == "list":
        campaign_id = str(payload.get("campaign_id", ""))
        ctx = await engine.get_context(campaign_id, user["id"])
        if not ctx:
            raise HTTPException(status_code=404, detail="Игра не найдена.")
        campaign, state, member, _ = ctx
        return {"items": _shop_items(load_pack(campaign["pack_id"]), state, member),
                "wallet": member["character"].get("currency", {})}
    return {"error": "UNKNOWN_ACTION"}


@app.get("/api/classes/{campaign_id}")
async def get_classes(campaign_id: str, user=Depends(current_user)):
    return await engine.class_offers(campaign_id, user["id"])


@app.post("/api/class/choose")
async def choose_class(payload: dict[str, Any], user=Depends(current_user)):
    campaign_id = str(payload.get("campaign_id", ""))
    class_id = str(payload.get("class_id", ""))
    if not class_id:
        return {"error": "INVALID", "message": "Класс не выбран."}
    return await engine.choose_class(campaign_id, user["id"], class_id)


@app.post("/api/settings")
async def save_settings(payload: dict[str, Any], user=Depends(current_user)):
    await db.execute(
        """INSERT INTO user_settings(user_id,payload) VALUES(?,?)
           ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload,updated_at=datetime('now')""",
        (user["id"], db.dumps(payload)),
    )
    return {"ok": True}


@app.post("/api/branches/{action}")
async def branches(action: str, payload: dict[str, Any], user=Depends(current_user)):
    campaign_id = str(payload.get("campaign_id", ""))
    rollback_step: int | None = None
    async with engine.mutate(campaign_id, user["id"]) as m:
        if m is None or m.member is None:
            raise HTTPException(status_code=404, detail="Игра не найдена.")

        if action == "save":
            name = str(payload.get("branch_id") or f"Ветка {m.step_no}")
            await m.conn.execute(
                "INSERT INTO saves(campaign_id,name,state_json) VALUES(?,?,?)",
                (campaign_id, name, db.dumps(m.state)),
            )
            return {"ok": True, "prose": "Страница сохранена."}

        if action == "rewind":
            step = m.step_no
            prev = await db.fetchone(
                "SELECT result_json FROM steps WHERE campaign_id=? AND step_no=?",
                (campaign_id, step - 1),
            )
            snapshot = db.loads(prev["result_json"], {}).get("state_before") if prev else None
            if not snapshot:
                initial = copy.deepcopy(m.pack["initial_state"])
                members = []
                for entry in m.state.get("members", []):
                    ch = copy.deepcopy(initial["character_template"])
                    ch["name"] = entry["character"].get("name", ch["name"])
                    members.append({**entry, "character": ch})
                snapshot = {"world": initial["world"],
                            "presentation": initial.get("presentation", {}), "members": members}
            m.state = snapshot
            m.pending = [entry["user_id"] for entry in snapshot.get("members", [])
                         if entry.get("status", "active") == "active"
                         and entry["character"].get("alive", True)]
            rollback_step = m.step_no = max(1, step - 1)
            m.phase = "orders"

        elif action == "switch":
            save = await db.fetchone(
                "SELECT * FROM saves WHERE id=? AND campaign_id=?",
                (str(payload.get("branch_id", "")), campaign_id),
            )
            if not save:
                return {"error": "NOT_FOUND"}
            snapshot = db.loads(save["state_json"], {})
            m.state = snapshot
            m.pending = [entry["user_id"] for entry in snapshot.get("members", [])
                         if entry.get("status", "active") == "active"
                         and entry["character"].get("alive", True)]
            rollback_step = m.step_no
            m.phase = "orders"

    if rollback_step is not None:
        await engine.emit(campaign_id, None, "rollback", {"step_no": rollback_step})
    if action == "rewind":
        return {"ok": True, "prose": "Страницы повернулись назад."}
    if action == "switch":
        return {"ok": True, "prose": "Ты открыл другую ветку истории."}
    return {"error": "UNKNOWN_ACTION"}


# ---------------- friends, invites, journal ----------------

@app.get("/api/friends")
async def friends(user=Depends(current_user)):
    rows = await db.fetchall(
        """SELECT f.id,u.id AS user_id,u.username,f.status FROM friends f JOIN users u
           ON (u.id = f.user_b AND f.user_a = ?) OR (u.id = f.user_a AND f.user_b = ?)
           WHERE u.id != ?""", (user["id"], user["id"], user["id"]))
    return {"friends": rows}


@app.post("/api/friends/request")
async def friend_request(payload: dict[str, Any], user=Depends(current_user)):
    target = await db.fetchone("SELECT * FROM users WHERE username = ?", (str(payload.get("username", "")),))
    if not target or target["id"] == user["id"]:
        return {"error": "NOT_FOUND"}
    await db.execute("INSERT OR IGNORE INTO friends(user_a,user_b,status) VALUES(?,?,'pending')",
                     (user["id"], target["id"]))
    return {"ok": True}


@app.post("/api/friends/accept")
async def friend_accept(payload: dict[str, Any], user=Depends(current_user)):
    fid = int(payload.get("id", 0))
    await db.execute("UPDATE friends SET status='accepted' WHERE id=? AND user_b=?", (fid, user["id"]))
    return {"ok": True}


@app.get("/api/users/online")
async def online_users(user=Depends(current_user)):
    rows = await db.fetchall("SELECT id,username,role FROM users WHERE id != ? ORDER BY id LIMIT 200", (user["id"],))
    return {"users": rows}


@app.post("/api/invites")
async def invite(payload: dict[str, Any], user=Depends(current_user)):
    campaign_id = str(payload.get("campaign_id", ""))
    invitee = int(payload.get("user_id", 0))
    membership = await db.fetchone(
        "SELECT * FROM campaign_members WHERE campaign_id=? AND user_id=?", (campaign_id, user["id"]))
    if not membership or not membership["is_founder"]:
        return {"error": "FORBIDDEN", "message": "Приглашать может только основатель."}
    was_member = await db.fetchone(
        "SELECT 1 FROM campaign_members WHERE campaign_id=? AND user_id=?", (campaign_id, invitee))
    if not was_member:
        return {"error": "NOT_PREVIOUS_MEMBER", "message": "В старую игру можно вернуть только бывшего участника."}
    await db.execute("INSERT INTO invites(campaign_id,inviter,invitee) VALUES(?,?,?)",
                     (campaign_id, user["id"], invitee))
    return {"ok": True}


@app.get("/api/invites")
async def invites(user=Depends(current_user)):
    rows = await db.fetchall(
        """SELECT i.id,i.campaign_id,c.name AS campaign_name,u.username AS inviter
           FROM invites i JOIN campaigns c ON c.id=i.campaign_id
           JOIN users u ON u.id=i.inviter
           WHERE i.invitee=? AND i.status='pending'""", (user["id"],))
    return {"invites": rows}


@app.post("/api/invites/{invite_id}/accept")
async def accept_invite(invite_id: int, user=Depends(current_user)):
    inv = await db.fetchone("SELECT * FROM invites WHERE id=? AND invitee=? AND status='pending'",
                            (invite_id, user["id"]))
    if not inv:
        return {"error": "NOT_FOUND"}
    campaign_id = inv["campaign_id"]
    async with engine.mutate(campaign_id, user["id"]) as m:
        if m is None:
            return {"error": "NOT_FOUND"}
        await m.conn.execute("UPDATE invites SET status='accepted' WHERE id=?", (invite_id,))
        await m.conn.execute(
            """UPDATE campaign_members SET status='active',left_at=NULL
               WHERE campaign_id=? AND user_id=?""",
            (campaign_id, user["id"]),
        )
        for entry in m.state.get("members", []):
            if entry["user_id"] == user["id"]:
                entry["status"] = "active"
        if user["id"] not in m.pending:
            m.pending.append(user["id"])
    await engine.emit(campaign_id, None, "user_joined", {
        "name": (await engine._user_names([user["id"]]))[0],
        "pending": await engine._user_names(m.pending),
    })
    return {"ok": True, "campaign_id": campaign_id}


@app.post("/api/detail/{detail_id}/accept")
async def accept_detail(detail_id: int, payload: dict[str, Any], user=Depends(current_user)):
    status = "canon" if payload.get("accept", True) else "rejected"
    row = await db.fetchone("SELECT campaign_id FROM ledger WHERE id=?", (detail_id,))
    if not row:
        return {"error": "NOT_FOUND"}
    async with campaign_lock(row["campaign_id"]):
        await db.execute("UPDATE ledger SET status=? WHERE id=?", (status, detail_id))
    return {"ok": True}


@app.post("/api/client-log")
async def client_log(payload: dict[str, Any], user=Depends(current_user)):
    await db.execute("INSERT INTO client_logs(user_id,level,message,payload) VALUES(?,?,?,?)",
                     (user["id"], str(payload.get("level", "info"))[:20],
                      str(payload.get("message", ""))[:500], db.dumps(payload.get("payload", {}))[:4000]))
    return {"ok": True}


# ---------------- websocket ----------------

@app.websocket("/ws/campaign/{campaign_id}")
async def ws_campaign(ws: WebSocket, campaign_id: str, token: str = ""):
    # Prefer the WebSocket subprotocol: a query string leaks the token into
    # proxy logs and browser history. The query parameter stays only for older
    # clients that have not received the new bundle yet.
    subprotocol = ""
    if not token:
        offered = str(ws.headers.get("sec-websocket-protocol", ""))
        for part in offered.split(","):
            value = part.strip()
            if value and value.lower() not in ("argo", "text-world"):
                subprotocol = value
                token = value
                break
    user = await auth.user_from_token(token)
    if not user:
        await ws.close(code=4401)
        return
    ctx = await engine.get_context(campaign_id, user["id"])
    if not ctx:
        await ws.close(code=4404)
        return
    await manager.connect(campaign_id, user["id"], ws, subprotocol or None)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(campaign_id, user["id"], ws)


# ---------------- admin ----------------

ADMIN_HTML = """<!doctype html><meta charset="utf-8"><title>Text World — operator</title>
<style>body{background:#0e0d0b;color:#e8decc;font:14px monospace;padding:24px}
a{color:#c9a227}h1{font-size:20px}pre{background:#17130f;padding:12px;overflow:auto}</style>
<h1>Text World / Серый кардинал</h1>
<p><a href="/api/admin/doctor">doctor</a> · <a href="/api/admin/logs">logs</a> · <a href="/api/admin/packs">packs</a> · <a href="/api/health">health</a></p>
<p>Логи игроку недоступны. Здесь — всё.</p>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(user=Depends(current_user)):
    _admin(user)
    return ADMIN_HTML


@app.get("/api/admin/doctor")
async def doctor(user=Depends(current_user)):
    _admin(user)
    issues: list[str] = []
    campaigns = await db.fetchall("SELECT * FROM campaigns")
    for c in campaigns:
        state = db.loads(c["state_json"], {})
        for m in state.get("members", []):
            ch = m["character"]
            if not ch.get("alive", True):
                continue
            if any(part.get("status") in ("destroyed", "severed") and part.get("vital")
                   for part in ch.get("body", {}).values() if isinstance(part, dict)):
                issues.append(f"{c['id']}: мёртвое тело ходит")
        if c["phase"] not in ("orders", "resolving", "published"):
            issues.append(f"{c['id']}: неизвестная фаза {c['phase']}")
    for pack_id in list_packs():
        issues.extend(f"[pack {pack_id}] {e}" for e in validate_pack(pack_id))
    return {"ok": not issues, "issues": issues, "campaigns": len(campaigns)}


@app.get("/api/admin/logs")
async def admin_logs(limit: int = 50, user=Depends(current_user)):
    _admin(user)
    return {
        "llm": await db.fetchall("SELECT * FROM llm_logs ORDER BY id DESC LIMIT ?", (limit,)),
        "audit": await db.fetchall("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)),
        "client": await db.fetchall("SELECT * FROM client_logs ORDER BY id DESC LIMIT ?", (limit,)),
    }


@app.get("/api/admin/packs")
async def admin_packs(user=Depends(current_user)):
    _admin(user)
    return {p: validate_pack(p) for p in list_packs()}


@app.post("/api/admin/pack/reload")
async def admin_reload(payload: dict[str, Any], user=Depends(current_user)):
    _admin(user)
    invalidate(str(payload.get("pack_id", "")) or None)
    return {"ok": True}
