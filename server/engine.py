from __future__ import annotations

import asyncio
import copy
import logging
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from . import catalog, db, gates, limits, models, worldsim
from .config import settings
from .dto import (anchor_view, character_view, faction_view, knowledge_view,
                  npc_view, presentation_view, preview_view, refuge_view)
from .locks import campaign_lock
from .packs import load_pack
from .provider import (INTENT_SYSTEM, NARRATOR_SYSTEM, NARRATOR_SYSTEM_LOCAL,
                       SUMMARY_SYSTEM, extract_json, provider)
from .realtime import manager
from .resolver import preview as calc_preview
from .resolver import resolve as resolve_action

log = logging.getLogger(__name__)


class ResolutionConflict(Exception):
    """The step was changed by another actor while we were resolving it."""

_META_MARKERS = (
    "мне нужно", "нужно написать", "пользователь", "факты:", "сначала",
    "хорошо,", "проверю", "нельзя добавлять", "я должен", "задача:",
)


_GENERIC_ACTION_PREFIXES = (
    "ты обводишь взглядом",
    "ты ждёшь",
    "ты откликаешься на происходящее",
    "ничего не изменилось",
)


def _tidy(text: str) -> str:
    """Collapse engine join artifacts without touching real ellipses."""
    return re.sub(r"(?<!\.)\.\.(?!\.)", ".", str(text or ""))


def _sentence(text: str) -> str:
    value = " ".join(str(text or "").split()).strip()
    if not value:
        return ""
    value = _tidy(value).rstrip(" .;:,")
    return value + "." if value else ""


def _join_sentences(parts: list[str]) -> str:
    seen: list[str] = []
    for part in parts:
        sentence = _sentence(part)
        if sentence and sentence not in seen:
            seen.append(sentence)
    return " ".join(seen)


def _clean_prose(text: str, fallback: str) -> str:
    raw = str(text or "")
    raw = re.sub(r"```.*?```", " ", raw, flags=re.DOTALL)
    raw = raw.replace("**", "").replace("*", "").replace("`", "")
    lines = [line.strip(" \t-—•\"'«»“”") for line in raw.split("\n") if line.strip()]
    prose = [_tidy(line) for line in lines
             if not any(m in line.lower() for m in _META_MARKERS)]
    return "\n\n".join(prose) if prose else fallback


_LEAK_LABELS = re.compile(
    r"(?:КРАТКАЯ ПАМЯТЬ МИРА|КРАТКАЯ ПАМЯТЬ|ПАМЯТЬ:|ТОН:|ПРОИЗОШЛО|РАНЕЕ|"
    r"СЛОВА ИГРОКА|STYLE:|ALLOWED:|entities_used|"
    r"Thinking Process|SYSTEM:|USER:)", re.IGNORECASE,
)


def _latin_ratio(text: str) -> float:
    letters = [char for char in str(text or "") if char.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if char.isascii()) / len(letters)


def _looping(sentence: str) -> bool:
    """A small model can fall into a word loop; such a sentence is not prose."""
    words = re.findall(r"[а-яё]+", sentence.lower())
    if len(words) < 7:
        return False
    trigrams = [" ".join(words[index:index + 3]) for index in range(len(words) - 2)]
    return any(trigrams.count(item) > 1 for item in set(trigrams))


_GROUND_STOP = frozenset("""
    этот эта эти того тому такой такая такое такие сам сама само сами моя моё мой мои
    твой твоя твоё твои свой своя своё свои наш наша наше наши ваш ваша ваше ваши
    уже ещё только тоже вот тут там здесь туда сюда оттуда когда где куда почему зачем
    потому поэтому если хотя пусть может будто словно вдруг внезапно даже
    стал стала стало стали быть был была было были есть будет будут весьма совсем
    почти чуть очень снова опять потом затем сейчас между после перед через
""".split())


def _grounded_prose(text: str, source: str) -> str:
    """Drop sentences whose content words are not in the facts we supplied.

    The local model sometimes invents a dog, a key or a snowstorm. Such a
    sentence is worse than no sentence at all: the engine can supply a canon
    description instead. The check is deliberately rough (four-letter stems,
    ё treated as е) so normal Russian inflections still match.
    """
    normalized = str(source).lower().replace("ё", "е")
    stems = {word[:4] for word in re.findall(r"[а-яё]{4,}", normalized)}
    known_names = set(re.findall(r"[а-яёё]{3,}", normalized)) | {
        "ты", "система", "москва", "арго", "чертаново",
    }
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", str(text or "")):
        words = [word.replace("ё", "е") for word in re.findall(r"[а-яё]{5,}", sentence.lower())
                 if word not in _GROUND_STOP]
        if not words:
            continue
        # A small model happily invents a name mid-sentence. Capitalised words
        # that are not in the supplied facts are never allowed through.
        invented_names = [name.lower().replace("ё", "е") for name in
                          re.findall(r"\b[А-ЯЁ][а-яё]{2,}\b", sentence)]
        if any(name not in known_names and name[:4] not in stems
               for name in invented_names):
            continue
        hits = sum(1 for word in words if word[:4] in stems)
        # A one-word atmosphere line must be wholly grounded; with three or
        # more content words a single matching root is too weak on its own.
        if len(words) < 3 and hits < len(words):
            continue
        if hits / len(words) >= 0.4:
            kept.append(sentence.strip())
    return " ".join(kept)


def _polish_prose(text: str, fact: str) -> str:
    """Trim a small model's echoes without inventing anything."""
    raw = str(text or "").strip()
    value = _clean_prose(raw, "")
    value = _LEAK_LABELS.split(value, 1)[0]
    known = {_sentence(part) for part in re.split(r"(?<=[.!?])\s+", fact)}
    parts = re.split(r"(?<=[.!?])\s+", " ".join(value.split("\n")))
    # A stopped generation often leaves a dangling half-sentence; drop it.
    if parts and raw and raw[-1] not in ".!?…\"»":
        parts = parts[:-1]
    kept: list[str] = []
    for part in parts:
        sentence = _sentence(part)
        if not sentence or sentence in known or sentence in kept:
            continue
        lowered = sentence.lower()
        if lowered in ("json", "prose", "entities_used"):
            continue
        if _latin_ratio(sentence) > 0.25 or _looping(sentence):
            continue
        if any(term in lowered for term in ("фракц", "отношение", "система подключена",
                                            "уровень", "опыт +", "очки опыта")):
            continue
        kept.append(sentence)
    result = " ".join(kept)
    if len(result) < 25:
        return ""
    return result[:600]


def _too_similar(first: str, second: str) -> bool:
    words_a = set(re.findall(r"[а-яё]{4,}", str(first or "").lower()))
    words_b = set(re.findall(r"[а-яё]{4,}", str(second or "").lower()))
    if not words_a or not words_b:
        return False
    return len(words_a & words_b) / max(1, min(len(words_a), len(words_b))) > 0.78


def _fallback_prose(results: list[dict[str, Any]], world_facts: list[str],
                    state: dict[str, Any], member: dict[str, Any],
                    step_no: int = 0) -> str:
    """Readable, canon-only scene when the literary model is unavailable.

    It never invents objects or people: only the resolved action text, world
    events and the location's own description are used. The description
    sentence rotates with the step so repeated turns do not read identically.
    """
    own = [str(r.get("text", "")) for r in results
           if r.get("user_id") == member.get("user_id")]
    concrete = [text for text in own
                if text and not text.lower().startswith(_GENERIC_ACTION_PREFIXES)]
    base = concrete or own or [str(r.get("text", "")) for r in results]
    parts = ["Ты ждёшь." if text.lower().startswith("ты ждёшь") else text
             for text in base if text]
    loc_id = str((member.get("character") or {}).get("location", ""))
    location = next((item for item in state.get("world", {}).get("locations", [])
                     if item.get("id") == loc_id), {})
    description = [part.strip() for part in
                   re.split(r"(?<=[.!?])\s+", str(location.get("desc", ""))) if part.strip()]
    if description:
        pick = description[step_no % len(description)]
        if _sentence(pick) not in {_sentence(part) for part in parts}:
            parts.append(pick)
    parts += [str(item) for item in world_facts]
    text = _join_sentences(parts)[:600]
    return text or "Мир вокруг дышит тяжело и ждёт твоего решения."


async def _audit(user_id: int | None, action: str, payload: dict[str, Any]) -> None:
    await db.execute(
        "INSERT INTO audit_logs(user_id,action,payload) VALUES(?,?,?)",
        (user_id, action, db.dumps(payload)),
    )


def _audit_item(item_id: str) -> None:
    db.execute_sync(
        "INSERT INTO audit_logs(user_id,action,payload) VALUES(NULL,'invalid_item',?)",
        (db.dumps({"item_id": item_id}),),
    )


async def _user_names(user_ids: list[int]) -> list[str]:
    """Display names instead of internal user ids in every player-facing payload."""
    if not user_ids:
        return []
    placeholders = ",".join("?" for _ in user_ids)
    rows = await db.fetchall(
        f"SELECT id,username FROM users WHERE id IN ({placeholders})", tuple(user_ids)
    )
    names = {row["id"]: row["username"] for row in rows}
    return [names.get(uid, "Игрок") for uid in user_ids]


async def _store_event(conn: Any, campaign_id: str, user_id: int | None,
                       event: str, payload: dict[str, Any]) -> int:
    cur = await conn.execute(
        """INSERT INTO campaign_events(campaign_id,user_id,event,payload_json)
           VALUES(?,?,?,?)""",
        (campaign_id, user_id, event, db.dumps(payload)),
    )
    return int(cur.lastrowid or 0)


async def emit(campaign_id: str, user_id: int | None, event: str,
               payload: dict[str, Any], persist: bool = True,
               conn: Any = None) -> int | None:
    """Persist a player-safe event and deliver it over WS.

    Persisting inside the step transaction means a crash after commit cannot
    lose the notification: a reconnecting client can replay it from history.
    """
    seq: int | None = None
    if persist:
        statement = (
            """INSERT INTO campaign_events(campaign_id,user_id,event,payload_json)
               VALUES(?,?,?,?)""",
            (campaign_id, user_id, event, db.dumps(payload)),
        )
        if conn is not None:
            cur = await conn.execute(*statement)
            seq = int(cur.lastrowid or 0)
        else:
            seq = await db.execute(*statement)
    message = dict(payload)
    if seq is not None:
        message["event_id"] = seq
    if user_id is None:
        await manager.broadcast(campaign_id, event, message)
    else:
        await manager.send(campaign_id, user_id, event, message)
    return seq


def _xp_to_next(level: int) -> int:
    return max(1, 5 * (2 ** (max(1, level) - 1)))


def _normalize_character(pack: dict[str, Any], character: dict[str, Any]) -> None:
    attrs = character.setdefault("attributes", {})
    if "mag" not in attrs and "int" in attrs:
        attrs["mag"] = attrs.get("int", 10)
    if "end" not in attrs:
        attrs["end"] = attrs.get("vit", 10)
    for key in ("str", "agi", "vit", "end", "mag"):
        attrs.setdefault(key, 10)
    attrs.pop("int", None)

    character.setdefault("level", 1)
    character.setdefault("xp", 0)
    if not character.get("xp_to_next"):
        character["xp_to_next"] = _xp_to_next(int(character.get("level", 1)))
    character.setdefault("unspent_points", 0)
    character.setdefault("skill_points", 0)
    character.setdefault("alive", True)
    character.setdefault("conditions", [])
    character["conditions"] = [
        c if isinstance(c, dict) else {"id": str(c), "name": str(c)}
        for c in character.get("conditions", [])
    ]
    character.setdefault("skills", [])
    character.setdefault("perks", [])
    character.setdefault("statuses", [])
    character.setdefault("equipment", {})
    character.setdefault("currency", {"coins": 0})
    character.setdefault("class_id", None)
    character.setdefault("class_name", None)
    character.setdefault("class_category", None)
    character.setdefault("pending_class_selection", False)
    character.setdefault("pending_level_up", False)

    inventory = []
    for entry in character.get("inventory", []) or []:
        if isinstance(entry, str):
            entry = {"id": entry, "name": entry, "weight_kg": 0, "quantity": 1}
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id", ""))
        if not item_id:
            continue
        data = catalog.item(pack, item_id)
        if not data:
            log.warning("dropping unknown item %s from saved inventory", item_id)
            continue
        qty = int(entry.get("quantity", entry.get("qty", 1)) or 1)
        existing = next((x for x in inventory if x.get("id") == item_id), None)
        if existing:
            existing["quantity"] = int(existing.get("quantity", 1)) + qty
        else:
            inventory.append({
                "id": item_id,
                "name": str(data.get("name", item_id)),
                "weight_kg": float(data.get("weight_kg", 0) or 0),
                "quantity": qty,
            })
    character["inventory"] = inventory

    mag = int(attrs.get("mag", 10))
    strength = int(attrs.get("str", 10))
    derived = character.setdefault("derived", {})
    derived["mana_max"] = max(10, mag * 3)
    carry = strength * 3
    for entry in character.get("inventory", []):
        data = catalog.item(pack, entry.get("id")) or {}
        if data.get("carry_bonus"):
            carry += int(data["carry_bonus"])
    derived["carry_kg"] = carry
    derived["refuge_level"] = int((character.get("derived") or {}).get("refuge_level", 0) or 0)
    resources = character.setdefault("resources", {})
    resources["mana"] = min(int(resources.get("mana", derived["mana_max"])), int(derived["mana_max"]))

    body = character.setdefault("body", {})
    for part in ("head", "torso", "arm_l", "arm_r", "leg_l", "leg_r"):
        if part not in body:
            body[part] = {"status": "intact", "vital": part in ("head", "torso")}
        elif isinstance(body[part], str):
            body[part] = {"status": body[part], "vital": part in ("head", "torso")}


def _ensure_state(pack: dict[str, Any], state: dict[str, Any]) -> None:
    worldsim.ensure_living(pack, state)
    world = state.setdefault("world", {})
    if not world.get("locations"):
        world["locations"] = worldsim.initial_locations(pack, state)
    worldsim.refresh_locations(pack, state)
    world.setdefault("stash", [])
    state.setdefault("_journal_pending", [])
    for member in state.get("members", []):
        _normalize_character(pack, member.setdefault("character", {}))
    if state.get("members"):
        first = state["members"][0]
        world["location"] = world.get("location") or {}
        if not world["location"].get("id"):
            world["location"] = {"id": first["character"].get("location", "apartment"),
                                 "name": "Квартира в Чертанове", "coords": [0, 0, 0], "elevation_m": 0}
        world["nearby"] = worldsim.nearby_for(pack, state, first)


def _deadline_after(seconds: int) -> str:
    value = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=seconds)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


async def create_campaign(pack_id: str, name: str, creator: dict[str, Any],
                          invitees: list[dict[str, Any]],
                          model_id: str | None = None,
                          language: str | None = None,
                          character_name: str | None = None) -> str:
    pack = load_pack(pack_id)
    campaign_id = uuid.uuid4().hex[:12]
    initial = copy.deepcopy(pack["initial_state"])
    template = initial["character_template"]

    founder_name = " ".join(str(character_name or "").split())[:24].strip() or "Странник"
    members: list[dict[str, Any]] = []
    for index, user in enumerate([creator, *invitees]):
        character = copy.deepcopy(template)
        if index == 0:
            character["name"] = founder_name
        else:
            character["name"] = user.get("username") or "Выживший"
        _normalize_character(pack, character)
        members.append({"user_id": user["id"], "character": character,
                        "is_founder": user["id"] == creator["id"], "status": "active"})

    state = copy.deepcopy(initial)
    state["members"] = members
    _ensure_state(pack, state)
    world = state["world"]
    pending = [m["user_id"] for m in members]
    random.Random(f"{campaign_id}:pending").shuffle(pending)

    model_id = models.get(model_id).id
    async with db.transaction(immediate=True) as conn:
        for user in [creator, *invitees]:
            await limits.assert_campaign_slot_conn(conn, user["id"])
        await conn.execute(
            """INSERT INTO campaigns(id,pack_id,name,state_json,step_no,phase,pending_json,
                                     created_by,model_id,pack_version,language,order_deadline_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (campaign_id, pack_id, name, db.dumps(state), 1, "orders", db.dumps(pending),
             creator["id"], model_id,
             str(pack["manifest"].get("version", "")),
             language or str(pack["manifest"].get("language", "ru")),
             _deadline_after(settings.order_deadline_seconds)),
        )
        for member in members:
            await conn.execute(
                """INSERT INTO campaign_members(campaign_id,user_id,character_json,is_founder,status)
                   VALUES(?,?,?,?,'active')""",
                (campaign_id, member["user_id"], db.dumps(member["character"]),
                 1 if member["is_founder"] else 0),
            )
        await conn.execute(
            "INSERT INTO audit_logs(user_id,action,payload) VALUES(?,?,?)",
            (creator["id"], "campaign_created", db.dumps({"id": campaign_id, "pack": pack_id})),
        )
    return campaign_id


async def get_context(campaign_id: str, user_id: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    campaign = await db.fetchone("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if not campaign:
        return None
    membership = await db.fetchone(
        "SELECT * FROM campaign_members WHERE campaign_id = ? AND user_id = ? AND status='active'",
        (campaign_id, user_id),
    )
    if not membership:
        return None
    state = db.loads(campaign["state_json"], {})
    pack = load_pack(campaign["pack_id"])
    member = {"user_id": user_id, "character": db.loads(membership["character_json"], {}),
              "is_founder": bool(membership["is_founder"]), "status": membership["status"]}
    _ensure_state(pack, state)
    _normalize_character(pack, member["character"])
    for entry in state.get("members", []):
        if entry["user_id"] == user_id:
            entry["character"] = member["character"]
    return campaign, state, member, membership


def _journal_lines(pending: list[Any], members: list[dict[str, Any]]) -> list[str]:
    """Public chronicle lines: no player prefixes, no generic filler, no dupes."""
    names = {str((m.get("character") or {}).get("name", ""))
             for m in members if m.get("character")}
    seen: set[str] = set()
    result: list[str] = []
    for text in pending:
        value = str(text or "").strip()
        for name in names:
            if name and value.startswith(name + ":"):
                value = value[len(name) + 1:].strip()
                break
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


async def _flush_journal(campaign_id: str, state: dict[str, Any], step_no: int | None = None) -> None:
    lines = _journal_lines(state.pop("_journal_pending", []), state.get("members", []))
    for value in lines:
        await db.execute(
            "INSERT INTO ledger(campaign_id,text,source,status,step_no) VALUES(?,?,?,?,?)",
            (campaign_id, value, "system", "canon", step_no),
        )


async def _save_state(campaign_id: str, state: dict[str, Any], step_no: int,
                      pending: list[int], phase: str) -> None:
    async with db.transaction() as conn:
        for entry in state.get("members", []):
            await conn.execute(
                "UPDATE campaign_members SET character_json=? WHERE campaign_id=? AND user_id=?",
                (db.dumps(entry["character"]), campaign_id, entry["user_id"]),
            )
        await conn.execute(
            "UPDATE campaigns SET state_json=?,step_no=?,pending_json=?,phase=?,updated_at=datetime('now') WHERE id=?",
            (db.dumps(state), step_no, db.dumps(pending), phase, campaign_id),
        )


@asynccontextmanager
async def mutate(campaign_id: str, user_id: int | None = None):
    """Single entry point for every campaign state change.

    Re-reads fresh state under the campaign lock, runs the callback inside one
    IMMEDIATE transaction and persists members + campaign atomically. This is
    the fix for lost updates in endpoints that previously wrote a stale
    state_json from outside any lock.
    """
    async with campaign_lock(campaign_id):
        campaign = await db.fetchone("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
        if not campaign:
            yield None
            return
        state = db.loads(campaign["state_json"], {})
        pack = load_pack(campaign["pack_id"])
        _ensure_state(pack, state)
        member = None
        if user_id is not None:
            membership = await db.fetchone(
                "SELECT * FROM campaign_members WHERE campaign_id=? AND user_id=?",
                (campaign_id, user_id),
            )
            if membership:
                member = {
                    "user_id": user_id,
                    "character": db.loads(membership["character_json"], {}),
                    "is_founder": bool(membership["is_founder"]),
                    "status": membership["status"],
                }
                _normalize_character(pack, member["character"])
                for entry in state.get("members", []):
                    if entry["user_id"] == user_id:
                        entry["character"] = member["character"]
        ctx = SimpleNamespace(
            campaign=campaign, state=state, member=member, pack=pack,
            pending=db.loads(campaign["pending_json"], []),
            phase=campaign["phase"], step_no=int(campaign["step_no"]), conn=None,
        )
        async with db.transaction(immediate=True) as conn:
            ctx.conn = conn
            yield ctx
            if member is not None:
                for entry in ctx.state.get("members", []):
                    if entry["user_id"] == user_id:
                        entry["character"] = member["character"]
            for entry in ctx.state.get("members", []):
                await conn.execute(
                    "UPDATE campaign_members SET character_json=? WHERE campaign_id=? AND user_id=?",
                    (db.dumps(entry["character"]), campaign_id, entry["user_id"]),
                )
            exists = await conn.execute(
                "SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)
            )
            if await exists.fetchone() is not None:
                cur = await conn.execute(
                    """UPDATE campaigns SET state_json=?,step_no=?,pending_json=?,phase=?,
                       updated_at=datetime('now')
                       WHERE id=? AND step_no=? AND phase=?""",
                    (db.dumps(ctx.state), ctx.step_no, db.dumps(ctx.pending), ctx.phase,
                     campaign_id, campaign["step_no"], campaign["phase"]),
                )
                if cur.rowcount != 1:
                    raise ResolutionConflict()


def _deterministic_intent(raw_text: str, pack: dict[str, Any],
                          nearby: list[dict[str, Any]],
                          allow_ambiguous: bool = False) -> dict[str, Any] | None:
    """Fast path: match supplied verbs without any LLM call."""
    lowered = raw_text.lower()
    matches: list[tuple[int, str]] = []
    for action_id, action in pack["actions"].items():
        for verb in action.get("verbs", []):
            if verb.lower() in lowered:
                matches.append((len(verb), action_id))
                break
    if not matches:
        return None
    matches.sort(reverse=True)
    if (len(matches) > 1 and matches[0][0] == matches[1][0]
            and matches[0][1] != matches[1][1] and not allow_ambiguous):
        return None  # ambiguous
    action_id = matches[0][1]
    target = None
    def _hits(text: str) -> bool:
        text = text.lower().strip()
        if len(text) < 3:
            return False
        if text in lowered:
            return True
        # Russian inflections: 'дверь' -> 'двери', 'зомби' -> 'зомби'
        stem = text[:4]
        return len(stem) >= 3 and stem in lowered

    for item in nearby:
        names = [str(item.get("name", "")).lower()] + [str(a).lower() for a in item.get("aliases", [])]
        if any(_hits(n) for n in names):
            target = item.get("id")
            break
    if pack["actions"][action_id].get("needs_target") and not target:
        return None
    matched_ids = {aid for _, aid in matches}
    if "move" in matched_ids and target and "attack" not in matched_ids:
        # «Открыть дверь и выйти» — это переход, а не обыск двери.
        action_id = "move"
    tags: list[str] = []
    if "ремень" in lowered or "пояс" in lowered:
        tags.append("using_belt")
    if "перил" in lowered or "перила" in lowered:
        tags.append("using_railing")
    if "прицел" in lowered or "целиться" in lowered:
        tags.append("aim")
    return {
        "action_id": action_id, "target": target, "method_tags": tags,
        "confidence": 0.95, "clarifying_question": None, "raw_text": raw_text,
    }


async def parse_intent(pack: dict[str, Any], raw_text: str, state: dict[str, Any],
                       member: dict[str, Any], spec: models.ModelSpec | None = None,
                       campaign_id: str | None = None,
                       step_no: int | None = None,
                       language: str = "ru") -> dict[str, Any]:
    nearby_items = worldsim.nearby_for(pack, state, member)
    nearby = [{"id": item.get("id"), "name": item.get("name"),
               "aliases": item.get("aliases", [])}
              for item in nearby_items]
    deterministic = _deterministic_intent(raw_text, pack, nearby)
    if deterministic:
        return deterministic
    lowered_text = raw_text.lower()
    verb_actions = {aid for aid, action in pack["actions"].items()
                    if any(str(verb).lower() in lowered_text
                           for verb in action.get("verbs", []))}
    if len(verb_actions) == 1:
        only = next(iter(verb_actions))
        if pack["actions"][only].get("needs_target"):
            # A known verb with no reachable target cannot be fixed by a model:
            # it cannot invent a nearby object. Answer in the world, without
            # the multi-second intent round trip.
            return {
                "action_id": "react", "target": None, "method_tags": [],
                "confidence": 0.9, "clarifying_question": None, "raw_text": raw_text,
            }
    actions = {aid: a.get("verbs", [])[:3] for aid, a in pack["actions"].items()}
    system = (INTENT_SYSTEM.replace("__ACTIONS__", db.dumps(actions))
              .replace("__NEARBY__", db.dumps(nearby))
              .replace("__LANGUAGE__", language))
    raw = await provider.complete(
        "intent", [
            {"role": "system", "content": system},
            {"role": "user", "content": raw_text},
        ],
        json_mode=True, max_tokens=180, spec=spec,
        timeout=spec.intent_timeout if spec else None,
        campaign_id=campaign_id, step_no=step_no,
        mask_terms=(str(member["character"].get("name", "")),),
    )
    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        parsed = {}
    def _freeform() -> dict[str, Any]:
        if len(raw_text.strip()) < 3:
            return {
                "action_id": "", "target": None, "method_tags": [], "confidence": 0.0,
                "clarifying_question": "Напиши чуть подробнее, что делаешь.",
                "raw_text": raw_text,
            }
        # AI Dungeon behavior: any text becomes a narrated reaction. No effects
        # are applied — the code still decides what actually changes in the world.
        return {
            "action_id": "react", "target": None, "method_tags": [], "confidence": 0.9,
            "clarifying_question": None, "raw_text": raw_text,
        }

    if not raw or not parsed:
        fallback = _deterministic_intent(raw_text, pack, nearby, allow_ambiguous=True)
        if fallback:
            return fallback
        return _freeform()
    action_id = str(parsed.get("action_id", ""))
    if action_id not in pack["actions"]:
        lowered = raw_text.lower()
        found = None
        for aid, action in pack["actions"].items():
            if any(verb.lower() in lowered for verb in action.get("verbs", [])):
                found = aid
                break
        if not found:
            question = parsed.get("clarifying_question")
            if question and float(parsed.get("confidence", 0) or 0) < 0.6:
                # The interpreter did not understand the order and asked a
                # question. Keep that state instead of silently turning it
                # into an empty reaction.
                return {
                    "action_id": "", "target": None, "method_tags": [],
                    "confidence": 0.0, "clarifying_question": str(question),
                    "raw_text": raw_text,
                }
            return _freeform()
        target = None
        for item in nearby:
            if item.get("name", "").lower() in raw_text.lower() or item.get("id", "") in raw_text.lower():
                target = item.get("id")
                break
        if pack["actions"][found].get("needs_target") and not target:
            return _freeform()
        action_id = found
    target = parsed.get("target") if parsed.get("target") else None
    if not target:
        for item in nearby:
            if item.get("name", "").lower() in raw_text.lower() or item.get("id", "") in raw_text.lower():
                target = item.get("id")
                break
    return {
        "action_id": action_id,
        "target": target,
        "method_tags": [str(t) for t in parsed.get("method_tags", [])][:4],
        "confidence": float(parsed.get("confidence", 0.8)) if action_id else 0.0,
        "clarifying_question": parsed.get("clarifying_question") or None,
        "raw_text": raw_text,
    }


async def _load_idempotent(key: str, campaign_id: str, user_id: int) -> tuple[str, dict[str, Any] | None]:
    """Returns 'ok' with a cached response, or 'miss'/'mismatch'."""
    from datetime import datetime, timezone

    row = await db.fetchone("SELECT * FROM turn_idempotency WHERE key=?", (key,))
    if not row:
        return "miss", None
    try:
        created = datetime.strptime(str(row["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expired = (now - created).total_seconds() > settings.idempotency_ttl_hours * 3600
    except ValueError:
        expired = True
    if expired:
        await db.execute("DELETE FROM turn_idempotency WHERE key=?", (key,))
        return "miss", None
    if row["campaign_id"] != campaign_id or row["user_id"] != user_id:
        return "mismatch", None
    return "ok", db.loads(row["response_json"], {})


async def _load_clarification(campaign_id: str, user_id: int, step_no: int,
                              clarification_id: str | None) -> dict[str, Any] | None:
    """Return a live clarification; expire stale ones lazily."""
    if not clarification_id:
        return None
    await db.execute(
        """UPDATE order_clarifications SET status='expired'
           WHERE campaign_id=? AND user_id=? AND step_no=? AND status='pending'
             AND expires_at<=datetime('now')""",
        (campaign_id, user_id, step_no),
    )
    row = await db.fetchone(
        """SELECT * FROM order_clarifications
           WHERE id=? AND campaign_id=? AND user_id=? AND step_no=?""",
        (clarification_id, campaign_id, user_id, step_no),
    )
    if not row:
        return None
    if row["status"] == "pending":
        return row
    # A closed clarification keeps its cached reply so a repeated answer is
    # idempotent and cannot create a second order.
    if db.loads(row.get("answer_response_json"), None) is not None:
        return row
    return None


async def _save_clarification(campaign_id: str, user_id: int, step_no: int,
                              original_text: str, question: str) -> str:
    clarification_id = uuid.uuid4().hex
    await db.execute(
        """INSERT INTO order_clarifications
               (id,campaign_id,user_id,step_no,original_text,question,status,expires_at)
           VALUES(?,?,?,?,?,?,'pending',?)""",
        (clarification_id, campaign_id, user_id, step_no, str(original_text).strip(),
         str(question), _deadline_after(settings.order_deadline_seconds)),
    )
    return clarification_id


async def _close_clarification(clarification_id: str, response: dict[str, Any]) -> None:
    """Cache the reply so a repeated answer cannot create a second order."""
    await db.execute(
        """UPDATE order_clarifications SET status='answered',answer_response_json=?
           WHERE id=? AND status='pending'""",
        (db.dumps(response), clarification_id),
    )


async def cancel_clarification(campaign_id: str, user_id: int,
                               clarification_id: str) -> dict[str, Any]:
    row = await db.fetchone(
        """SELECT * FROM order_clarifications
           WHERE id=? AND campaign_id=? AND user_id=?""",
        (clarification_id, campaign_id, user_id),
    )
    campaign = await db.fetchone("SELECT step_no,phase FROM campaigns WHERE id=?", (campaign_id,))
    if (not row or row["status"] != "pending" or not campaign
            or int(row["step_no"]) != int(campaign["step_no"])
            or campaign["phase"] != "orders"):
        return {"error": "CLARIFICATION_EXPIRED",
                "message": "Уточнение больше не актуально."}
    response = {"order_saved": False, "message": "Уточнение отменено."}
    await _close_clarification(str(row["id"]), response)
    return response


async def submit_order(campaign_id: str, user_id: int, raw_text: str,
                       idempotency_key: str | None = None,
                       defer_resolution: bool = False,
                       clarification_id: str | None = None) -> dict[str, Any]:
    if len(raw_text) > settings.turn_max_chars:
        return {"error": "TEXT_TOO_LONG", "message": "Приказ слишком длинный."}

    if idempotency_key:
        state_name, cached = await _load_idempotent(idempotency_key, campaign_id, user_id)
        if state_name == "mismatch":
            return {"error": "IDEMPOTENCY_MISMATCH", "message": "Ключ приказа уже использован."}
        if state_name == "ok":
            return cached

    ctx = await get_context(campaign_id, user_id)
    if not ctx:
        return {"error": "NOT_IN_CAMPAIGN"}
    campaign, state, member, _ = ctx
    character = member["character"]
    step_no = int(campaign["step_no"])
    clarification = None
    if clarification_id:
        clarification = await _load_clarification(
            campaign_id, user_id, step_no, str(clarification_id)
        )
        if not clarification:
            return {"error": "CLARIFICATION_EXPIRED",
                    "message": "Уточнение больше не актуально. Напиши приказ заново."}
        cached = db.loads(clarification.get("answer_response_json"), None)
        if isinstance(cached, dict):
            return cached
        # The reply continues the original order instead of becoming a new one.
        raw_text = f"{clarification['original_text']} {raw_text}".strip()
        if len(raw_text) > settings.turn_max_chars:
            return {"error": "TEXT_TOO_LONG", "message": "Приказ слишком длинный."}
    if character.get("pending_class_selection"):
        return {"error": "CLASS_SELECTION_REQUIRED", "message": "Сначала выбери класс."}
    if campaign["phase"] != "orders" or user_id not in db.loads(campaign["pending_json"], []):
        return {"error": "NOT_YOUR_TURN", "message": "Сейчас не твой ход."}

    pack = load_pack(campaign["pack_id"])
    spec = models.get(campaign.get("model_id"))
    order = await parse_intent(pack, raw_text, state, member, spec,
                               campaign_id, int(campaign["step_no"]),
                               str(campaign.get("language") or "ru"))
    if order["confidence"] < 0.6 and order.get("clarifying_question"):
        question = str(order["clarifying_question"])
        new_id = await _save_clarification(
            campaign_id, user_id, step_no, raw_text, question
        )
        response = {"clarifying_question": question, "clarification_id": new_id,
                    "order_saved": False}
        if clarification:
            # A repeated answer must land on the newest question, not spawn a
            # second independent order.
            await _close_clarification(str(clarification["id"]), response)
        return response

    prev = calc_preview(pack, state, member, order)
    public_prev = preview_view(prev)
    response: dict[str, Any] = {
        "order_saved": True, "preview": public_prev, "waiting_for": [],
        "step_no": int(campaign["step_no"]),
    }
    resolve_needed = False

    async with campaign_lock(campaign_id):
        fresh = await db.fetchone(
            "SELECT step_no,phase,pending_json FROM campaigns WHERE id=?", (campaign_id,)
        )
        if not fresh:
            return {"error": "NOT_IN_CAMPAIGN"}
        if fresh["phase"] == "resolving":
            return {"error": "STEP_RESOLVING", "message": "Мир ещё обрабатывает предыдущий шаг."}
        if fresh["phase"] != "orders" or int(fresh["step_no"]) != int(campaign["step_no"]):
            return {"error": "STEP_CHANGED", "message": "Шаг изменился. Отправь приказ заново."}
        pending_now = db.loads(fresh["pending_json"], [])
        if user_id not in pending_now:
            return {"error": "NOT_YOUR_TURN", "message": "Сейчас не твой ход."}

        if idempotency_key:
            state_name, cached = await _load_idempotent(idempotency_key, campaign_id, user_id)
            if state_name == "mismatch":
                return {"error": "IDEMPOTENCY_MISMATCH", "message": "Ключ приказа уже использован."}
            if state_name == "ok":
                return cached

        pending = [p for p in pending_now if p != user_id]
        response["waiting_for"] = await _user_names(pending)
        response["my_turn"] = not pending
        async with db.transaction(immediate=True) as conn:
            await conn.execute(
                """INSERT OR REPLACE INTO orders(campaign_id,step_no,user_id,action_json,preview_json)
                   VALUES(?,?,?,?,?)""",
                (campaign_id, campaign["step_no"], user_id, db.dumps(order), db.dumps(prev)),
            )
            await conn.execute(
                "UPDATE campaigns SET pending_json=? WHERE id=?",
                (db.dumps(pending), campaign_id),
            )
            if idempotency_key and response.get("order_saved"):
                await conn.execute(
                    """INSERT INTO turn_idempotency(key,campaign_id,user_id,response_json)
                       VALUES(?,?,?,?)""",
                    (idempotency_key, campaign_id, user_id, db.dumps(response)),
                )
            if clarification:
                await conn.execute(
                    """UPDATE order_clarifications SET status='answered',answer_response_json=?
                       WHERE id=? AND status='pending'""",
                    (db.dumps(response), str(clarification["id"])),
                )
        resolve_needed = not pending

    submitter = (await _user_names([user_id]))[0]
    await emit(campaign_id, None, "awaiting_orders", {
        "step_no": campaign["step_no"], "pending": response["waiting_for"],
        "submitted": submitter,
    })
    await emit(campaign_id, None, "order_submitted", {
        "step_no": campaign["step_no"], "submitted": submitter,
        "pending": response["waiting_for"],
    })
    if resolve_needed:
        if defer_resolution:
            # The literary model can take tens of seconds. Never keep the
            # player's HTTP request open that long: resolve in the background
            # and let the client follow /step/status and the event feed.
            response["needs_resolution"] = True
        else:
            await _resolve_step(campaign_id, expected_step_no=int(campaign["step_no"]))
    return response


def _add_item(character: dict[str, Any], pack: dict[str, Any], item_id: str, qty: int = 1) -> bool:
    if not item_id or qty <= 0:
        return False
    data = catalog.item(pack, item_id)
    if not data:
        log.warning("refusing unknown item id %s", item_id)
        _audit_item(item_id)
        return False
    inventory = character.setdefault("inventory", [])
    existing = next((entry for entry in inventory if entry.get("id") == item_id), None)
    if existing:
        existing["quantity"] = int(existing.get("quantity", 1)) + qty
        return True
    inventory.append({
        "id": item_id,
        "name": str(data.get("name", item_id)),
        "weight_kg": float(data.get("weight_kg", 0) or 0),
        "quantity": qty,
    })
    return True


def _remove_item(character: dict[str, Any], item_id: str, qty: int = 1,
                 pack: dict[str, Any] | None = None) -> bool:
    if pack is not None and not catalog.item(pack, item_id):
        log.warning("refusing to remove unknown item id %s", item_id)
        _audit_item(item_id)
        return False
    inventory = character.setdefault("inventory", [])
    entry = next((x for x in inventory if x.get("id") == item_id), None)
    if not entry or int(entry.get("quantity", 1)) < qty:
        return False
    entry["quantity"] = int(entry.get("quantity", 1)) - qty
    if entry["quantity"] <= 0:
        inventory.remove(entry)
    return True


def _level_up(pack: dict[str, Any], character: dict[str, Any]) -> list[int]:
    rules = pack.get("rules", {})
    gained: list[int] = []
    while int(character.get("xp", 0)) >= int(character.get("xp_to_next", 5)):
        character["xp"] = int(character.get("xp", 0)) - int(character.get("xp_to_next", 5))
        character["level"] = int(character.get("level", 1)) + 1
        character["xp_to_next"] = _xp_to_next(int(character["level"]))
        character["unspent_points"] = int(character.get("unspent_points", 0)) + int(rules.get("attribute_points_per_level", 2))
        if int(character["level"]) in set(rules.get("skill_point_levels", [5, 10, 15])):
            character["skill_points"] = int(character.get("skill_points", 0)) + 1
        character["pending_level_up"] = True
        gained.append(int(character["level"]))
    return gained


def _add_condition(pack: dict[str, Any], character: dict[str, Any], cid: str) -> None:
    conditions = character.setdefault("conditions", [])
    if cid in [c.get("id") if isinstance(c, dict) else c for c in conditions]:
        return
    data = (pack.get("conditions", {}) or {}).get(cid) or {}
    entry: dict[str, Any] = {"id": cid, "name": data.get("name", cid)}
    if data.get("duration_turns") is not None:
        entry["turns"] = int(data["duration_turns"])
    conditions.append(entry)


def _remove_condition(character: dict[str, Any], cid: str) -> None:
    character["conditions"] = [c for c in character.get("conditions", [])
                               if (c.get("id") if isinstance(c, dict) else c) != cid]


def _begin_class_selection(state: dict[str, Any]) -> None:
    """Single source of truth: character.pending_class_selection."""
    for member in state.get("members", []):
        character = member.get("character", {})
        if member.get("status", "active") != "active" or not character.get("alive", True):
            continue
        character["pending_class_selection"] = True
    state.setdefault("flags", {})["class_offered"] = True
    state.setdefault("_journal_pending", []).append(
        "Первый заражённый упал. Система открыла список классов."
    )


def _apply_effects(pack: dict[str, Any], state: dict[str, Any], member: dict[str, Any],
                   effects: dict[str, Any], order: dict[str, Any] | None = None,
                   rules: dict[str, Any] | None = None, seed: str = "") -> dict[str, Any]:
    character = member["character"]
    _normalize_character(pack, character)
    facts: dict[str, Any] = {}
    ui_events: list[str] = []

    for cid in effects.get("add_conditions", []) or []:
        _add_condition(pack, character, str(cid))
    for cid in effects.get("remove_conditions", []) or []:
        _remove_condition(character, str(cid))

    if effects.get("move_to"):
        character["location"] = effects["move_to"]
        loc = next((l for l in state.get("world", {}).get("locations", [])
                    if l.get("id") == effects["move_to"]), None)
        if loc:
            loc["visited"] = True
        facts["moved_to"] = (loc or {}).get("name", effects["move_to"])
    if effects.get("location"):
        character["location"] = effects["location"]

    if effects.get("xp"):
        character["xp"] = int(character.get("xp", 0)) + int(effects["xp"])
    levels = _level_up(pack, character)
    if levels:
        facts["levels"] = levels

    currency = character.setdefault("currency", {})
    for key in ("gold", "coins"):
        if effects.get(key):
            currency[key] = int(currency.get(key, 0)) + int(effects[key])

    for item in effects.get("inventory_add", []) or []:
        if isinstance(item, str):
            _add_item(character, pack, item, 1)
        elif isinstance(item, dict):
            _add_item(character, pack, str(item.get("item") or item.get("id")), int(item.get("qty", 1) or 1))
    for item in effects.get("inventory_remove", []) or []:
        _remove_item(character, str(item.get("item") if isinstance(item, dict) else item),
                     int(item.get("qty", 1)) if isinstance(item, dict) else 1, pack)

    for skill in effects.get("skills_add", []) or []:
        if skill.get("id") not in [s.get("id") for s in character.get("skills", [])]:
            character.setdefault("skills", []).append(skill)

    if effects.get("set_flags"):
        for flag in effects["set_flags"]:
            state.setdefault("flags", {})[str(flag)] = True
    if effects.get("clear_flags"):
        for flag in effects["clear_flags"]:
            state.setdefault("flags", {}).pop(str(flag), None)
    if effects.get("knowledge"):
        for item in effects["knowledge"]:
            if item not in state.setdefault("knowledge", []):
                state["knowledge"].append(item)

    if effects.get("combat_win"):
        target_id = (order or {}).get("target")
        target_item = next((item for item in worldsim.nearby_for(pack, state, member)
                            if item.get("id") == target_id or target_id in item.get("aliases", [])), None)
        mob_id = effects.get("mob_id") or (target_item or {}).get("mob")
        loc_id = character.get("location")
        if mob_id:
            worldsim.kill_mob(state, loc_id, str(mob_id))
            mob_data = catalog.mob(pack, str(mob_id)) or {}
            loot = worldsim.draw_loot(pack, str(effects.get("loot_table") or mob_data.get("loot_table", "zombie_common")),
                                      worldsim.rng_for(str(state.get("id", "")), 0, f"loot:{mob_id}:{seed}"))
            for drop in loot.get("items", []):
                _add_item(character, pack, drop["item"], drop.get("qty", 1))
            if loot.get("coins"):
                currency["coins"] = int(currency.get("coins", 0)) + int(loot["coins"])
            if loot.get("xp"):
                character["xp"] = int(character.get("xp", 0)) + int(loot["xp"])
                _level_up(pack, character)
            facts["defeated"] = mob_data.get("name", mob_id)
            tags = set(mob_data.get("tags", []))
            if ("infected" in tags or "zombie" in str(mob_id)) and not state.setdefault("flags", {}).get("first_kill"):
                state["flags"]["first_kill"] = True
                state["flags"]["met_zombie"] = True
                _begin_class_selection(state)
                facts["first_kill"] = True
    if effects.get("combat_lose"):
        _add_condition(pack, character, "bleeding")
        body = character.setdefault("body", {})
        part = "arm_l" if int(character.get("xp", 0)) % 2 == 0 else "leg_r"
        if isinstance(body.get(part), dict) and body[part].get("status") == "intact":
            body[part]["status"] = "wounded"

    if effects.get("defeat_target"):
        target_id = (order or {}).get("target")
        for item in worldsim.nearby_for(pack, state, member):
            if item.get("id") == target_id or target_id in item.get("aliases", []):
                if item.get("kind") == "mob":
                    worldsim.kill_mob(state, character.get("location"), str(item.get("mob")))
                break
    if effects.get("defeat_npc"):
        npc_id = str(effects["defeat_npc"])
        if npc_id in state.get("npcs", {}):
            state["npcs"][npc_id]["alive"] = False

    if effects.get("search"):
        loc = worldsim.current_location(state, member)
        if loc:
            table = loc.get("loot_table") or "street"
            loot = worldsim.draw_loot(pack, str(table), worldsim.rng_for(str(state.get("id", "")), 0, f"search:{loc.get('id')}:{seed}"))
            for drop in loot.get("items", []):
                _add_item(character, pack, drop["item"], drop.get("qty", 1))
            if loot.get("coins"):
                currency["coins"] = int(currency.get("coins", 0)) + int(loot["coins"])
            if loot.get("xp"):
                character["xp"] = int(character.get("xp", 0)) + int(loot["xp"])
                _level_up(pack, character)
            facts["searched"] = loc.get("name", "")
            if loc.get("loot_bonus"):
                loc["loot_bonus"] = max(0, int(loc["loot_bonus"]) - 1)
            for stash in [s for s in state.get("world", {}).get("stash", []) if s.get("location") == loc.get("id")]:
                _add_item(character, pack, str(stash.get("item")), int(stash.get("qty", 1)))
                state["world"]["stash"].remove(stash)
                facts["found_stash"] = True

    if effects.get("take"):
        target_id = (order or {}).get("target")
        item = next((x for x in worldsim.nearby_for(pack, state, member)
                     if x.get("id") == target_id or target_id in x.get("aliases", [])), None)
        if item and item.get("kind") == "object":
            item["taken"] = True
            facts["taken"] = item.get("name", "")

    if effects.get("use"):
        target_id = (order or {}).get("target")
        data = catalog.item(pack, target_id)
        if data:
            effect = data.get("effect", {}) or {}
            for cid in effect.get("remove_conditions", []) or []:
                _remove_condition(character, str(cid))
            if effect.get("heal"):
                for part, info in character.get("body", {}).items():
                    if isinstance(info, dict) and info.get("status") == "wounded":
                        info["status"] = "intact"
                        break
            if data.get("kind") == "consumable":
                _remove_item(character, str(target_id), 1, pack)
            facts["used"] = data.get("name", target_id)

    if effects.get("rest"):
        _remove_condition(character, "exhausted")
        _remove_condition(character, "fear")
        resources = character.setdefault("resources", {})
        resources["mana"] = int(character.get("derived", {}).get("mana_max", 30))
        if state.get("world", {}).get("rest_bonus"):
            _remove_condition(character, "bleeding")
            state["world"]["rest_bonus"] = False
        facts["rested"] = True

    if effects.get("craft"):
        recipe = _pick_recipe(pack, state, character)
        if recipe:
            for inp in recipe.get("inputs", []):
                data = catalog.item(pack, inp.get("item")) or {}
                if data.get("kind") != "tool":
                    _remove_item(character, str(inp.get("item")), int(inp.get("qty", 1)), pack)
            for out in recipe.get("output", []):
                _add_item(character, pack, str(out.get("item")), int(out.get("qty", 1)))
            if recipe.get("xp"):
                character["xp"] = int(character.get("xp", 0)) + int(recipe["xp"])
                _level_up(pack, character)
            facts["crafted"] = recipe.get("name", recipe.get("id"))
        else:
            facts["crafted"] = None

    if effects.get("build"):
        state.setdefault("world", {}).setdefault("structures", []).append({
            "location": character.get("location"), "name": "Укрепление", "step": state.get("step_no", 0),
        })
        facts["built"] = True

    if effects.get("treat"):
        for cid in ("bleeding", "poisoned"):
            _remove_condition(character, cid)
        for part, info in character.get("body", {}).items():
            if isinstance(info, dict) and info.get("status") == "wounded":
                info["status"] = "intact"
                break
        facts["treated"] = True

    if effects.get("cast"):
        resources = character.setdefault("resources", {})
        cost = 5
        if int(resources.get("mana", 0)) >= cost:
            resources["mana"] = int(resources.get("mana", 0)) - cost
            character["xp"] = int(character.get("xp", 0)) + 3
            _level_up(pack, character)
            facts["cast"] = True
        else:
            _add_condition(pack, character, "exhausted")
            facts["cast"] = False

    if effects.get("claim_refuge"):
        refuge = worldsim.claim_refuge(pack, state, member)
        if refuge:
            facts["refuge"] = refuge
            character["derived"]["refuge_level"] = int(refuge.get("level", 1))
            state.setdefault("_journal_pending", []).append(
                f"Убежище «{refuge.get('name')}» закреплено за тобой."
            )
        else:
            facts["refuge"] = None
    if effects.get("enter_refuge"):
        if state.get("refuge", {}).get("status") == "claimed":
            character["location"] = "refuge_core"
            facts["moved_to"] = "Ядро убежища"
    if effects.get("leave_refuge"):
        anchor = state.get("refuge", {}).get("anchor")
        if anchor:
            character["location"] = anchor
            facts["moved_to"] = "Выход из убежища"

    if effects.get("talk"):
        target_id = (order or {}).get("target")
        npc = state.get("npcs", {}).get(target_id or "")
        if npc:
            npc.setdefault("memory", []).append("разговор с игроком")
            npc["memory"] = npc["memory"][-8:]
            facts["talked_to"] = npc.get("name")
            rumor = (catalog.factions(pack).get(str(npc.get("faction", "")), {}) or {}).get("rumors", [])
            if rumor:
                state.setdefault("living", {}).setdefault("rumors", []).append(rumor[0])
                facts["rumor"] = rumor[0]
    if effects.get("help"):
        target_id = (order or {}).get("target")
        npc = state.get("npcs", {}).get(target_id or "")
        if npc:
            npc.setdefault("memory", []).append("игрок помог")
            npc["disposition"] = "дружелюбен"
            faction = str(npc.get("faction", "survivors"))
            if faction in state.get("factions", {}):
                state["factions"][faction]["rep"] = int(state["factions"][faction].get("rep", 0)) + 1
            facts["helped"] = npc.get("name")
    if effects.get("recruit"):
        target_id = (order or {}).get("target")
        npc = state.get("npcs", {}).get(target_id or "")
        if npc and npc.get("disposition") in ("дружелюбен", "нейтрален"):
            npc["following"] = True
            npc["location"] = character.get("location")
            facts["recruited"] = npc.get("name")
    if effects.get("listen"):
        rumors = state.get("living", {}).get("rumors", [])
        if rumors:
            facts["heard"] = rumors[-1]
    if effects.get("loot_reward"):
        loot = worldsim.draw_loot(pack, "zombie_common", worldsim.rng_for(str(state.get("id", "")), 0, f"reward:{seed}"))
        for drop in loot.get("items", []):
            _add_item(character, pack, drop["item"], drop.get("qty", 1))
        if loot.get("coins"):
            currency["coins"] = int(currency.get("coins", 0)) + int(loot["coins"])
        if loot.get("xp"):
            character["xp"] = int(character.get("xp", 0)) + int(loot["xp"])
            _level_up(pack, character)
        facts["looted"] = True
    if effects.get("flee"):
        loc = worldsim.current_location(state, member)
        if loc and loc.get("exits"):
            target = random.Random(f"{seed}:flee").choice(loc["exits"]).get("to")
            if target:
                character["location"] = target
                facts["moved_to"] = target
    if effects.get("ui"):
        ui_events.append(str(effects["ui"]))

    body = effects.get("body")
    if body:
        character.setdefault("body", {}).update(body)

    vital = (rules or {}).get("vital_parts", ["head", "torso"])
    for part in vital:
        info = character.get("body", {}).get(part)
        if isinstance(info, dict) and info.get("status") in ("destroyed", "severed"):
            character["alive"] = False
            _add_condition(pack, character, "dead")
            break

    _normalize_character(pack, character)
    facts["ui_events"] = ui_events
    return facts


def _pick_recipe(pack: dict[str, Any], state: dict[str, Any], character: dict[str, Any]) -> dict[str, Any] | None:
    inventory = {entry.get("id"): int(entry.get("quantity", 1)) for entry in character.get("inventory", [])}
    ctx_era = gates.era_index(str(state.get("era", "E0")))
    for recipe in catalog.recipes(pack).values():
        if gates.era_index(recipe.get("era", "E0")) > ctx_era:
            continue
        if all(inventory.get(str(inp.get("item")), 0) >= int(inp.get("qty", 1)) for inp in recipe.get("inputs", [])):
            return recipe
    return None


def _world_tick(pack: dict[str, Any], state: dict[str, Any], campaign_id: str, step_no: int) -> dict[str, Any]:
    result = worldsim.tick(pack, state, campaign_id, step_no)
    for member in state.get("members", []):
        for condition in member["character"].get("conditions", []):
            if condition.get("turns") is not None:
                condition["turns"] = int(condition["turns"]) - 1
        member["character"]["conditions"] = [
            c for c in member["character"].get("conditions", [])
            if c.get("turns") is None or int(c["turns"]) > 0
        ]
    return result


async def _narrate_one(member: dict[str, Any], state: dict[str, Any], pack: dict[str, Any],
                       results: list[dict[str, Any]], world_facts: list[str],
                       allowed: str, allowed_tokens: set[str], spec: models.ModelSpec,
                       campaign_id: str, step_no: int,
                       raw_text: str = "", recent_prose: list[str] | None = None,
                       summary: str = "", language: str = "ru") -> tuple[int, str, list[str]]:
    uid = member["user_id"]
    fact_lines = [str(r["text"]) for r in results]
    fact_lines += [str(item) for item in world_facts]
    fact = _join_sentences(fact_lines) or "Ничего не изменилось."
    living = state.get("living", {})
    continuation = "\n".join(recent_prose or [])
    intro = str((pack.get("manifest") or {}).get("intro", "")).strip()
    if not continuation and step_no <= 1 and intro:
        continuation = intro
    # Do not feed the model the same sentence twice: it mirrors it verbatim.
    if continuation and _sentence(continuation.split("\n")[-1]) == _sentence(fact):
        continuation = "\n".join(continuation.split("\n")[:-1])
    weather_names = {"rain": "дождь", "clear": "ясно", "overcast": "пасмурно",
                     "fog": "туман", "snow": "снег", "storm": "гроза"}
    time_names = {"day": "день", "morning": "утро", "evening": "вечер", "night": "ночь"}
    weather = weather_names.get(str(living.get("weather", "")), str(living.get("weather", "ясно")))
    day_time = time_names.get(str(living.get("time_of_day", "")), str(living.get("time_of_day", "день")))
    era_name = str((catalog.eras(pack).get("eras", {}) or {}).get(state.get("era", "E0"), {}).get("name", ""))
    short_allowed = ", ".join(list(dict.fromkeys(
        part.strip() for part in allowed.split(",") if part.strip()))[:10])
    cont = continuation.strip()
    if len(cont) > 240:
        cont = "\n".join(cont.split("\n")[-2:])[-240:]
    memory = summary.strip()[:160]
    style = str(pack["style"].get("system_prompt", "")).strip()
    if len(style) > 180:
        style = style[:177].rstrip(" ,.;") + "…"
    local = spec.provider == "ollama"
    if local:
        user_prompt = (
            f"Время: {day_time}, погода: {weather}, {living.get('temperature_c', '')} °C.\n"
            + (f"РАНЕЕ (продолжай, не повторяя):\n{cont}\n" if cont else "")
            + (f"ПАМЯТЬ: {memory}\n" if memory else "")
            + (f"СЛОВА ИГРОКА: {raw_text[:200]}\n" if raw_text else "")
            + f"ПРОИЗОШЛО: {fact[:420]}\n"
            + "Опиши этот ход."
        )
    else:
        user_prompt = (
            f"Эпоха: {era_name or state.get('era', 'E0')}. Время: {day_time}, "
            f"погода: {weather}, температура: {living.get('temperature_c', '')} °C.\n"
            + (f"РАНЕЕ (продолжи именно эту сцену, не повторяя её):\n{cont}\n" if cont else "")
            + (f"КРАТКАЯ ПАМЯТЬ МИРА: {memory}\n" if memory else "")
            + (f"СЛОВА ИГРОКА ЭТИМ ХОДОМ: {raw_text[:200]}\n" if raw_text else "")
            + f"ПРОИЗОШЛО: {fact[:420]}\n"
            + "Опиши сцену так, как её видит герой."
            + (" Продолжай предыдущий абзац." if cont else "")
            + "Если игрок написал вопрос или реплику — ответь средствами мира, "
            "не выдумывая новых фактов."
        )
    template = NARRATOR_SYSTEM_LOCAL if local else NARRATOR_SYSTEM
    system = (template.replace("__STYLE__", style)
              .replace("__ALLOWED__", short_allowed if local else allowed)
              .replace("__LANGUAGE__", language))
    # Previous prose is not evidence: otherwise a hallucinated detail becomes
    # "canon" on the next turn. Grounding uses only facts, the player's words
    # and the allowed objects supplied by the engine.
    grounding = " ".join((fact, raw_text[:200], allowed))
    own_parts: list[str] = []
    for result_row in results:
        if result_row.get("user_id") != uid:
            continue
        text = str(result_row.get("text", ""))
        if (result_row.get("action_id") == "move"
                and result_row.get("result") == "success"):
            # The resolver's line is the same in every corridor. Give the
            # reader the destination instead of repeating one stock phrase.
            loc = next((item for item in state.get("world", {}).get("locations", [])
                        if item.get("id") == str((member.get("character") or {}).get("location", ""))), {})
            detail = str(loc.get("desc", "")).split(".")[0].strip()
            if detail:
                text = "Ты проходишь дальше. " + _sentence(detail)
        own_parts.append(text)
    own_text = _join_sentences(own_parts)
    entities: list[str] = []
    prose = ""
    started = time.monotonic()
    scene_budget = spec.narrator_timeout + (10 if local else 15)

    def time_left() -> float:
        return scene_budget - (time.monotonic() - started)

    mask_terms = tuple(
        str(m["character"].get("name", "")) for m in state.get("members", [])
    )
    if local:
        # A small local model loses most of its token budget on JSON syntax and
        # then starts echoing prompt labels. Plain prose plus stop words is both
        # faster and cleaner.
        text = await provider.complete(
            "narrator",
            [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
            json_mode=False, max_tokens=spec.narrator_max_tokens, spec=spec,
            timeout=spec.narrator_timeout, campaign_id=campaign_id, step_no=step_no,
            mask_terms=mask_terms,
        )
        prose = _grounded_prose(_polish_prose(text, fact), grounding)
        retry_reason = ""
        if not prose and text.strip():
            retry_reason = ("Предыдущий ответ не подошёл. Напиши ровно два коротких "
                            "предложения по фактам, без повторов и выдумок.")
        elif prose and recent_prose and _too_similar(prose, recent_prose[0]):
            retry_reason = ("Предыдущая сцена уже описана. Не повторяй её слова, "
                            "опиши только то, что изменилось в ПРОИЗОШЛО.")
        if retry_reason and time_left() > 6:
            retry_text = await provider.complete(
                "narrator",
                [{"role": "system", "content": system},
                 {"role": "user", "content": user_prompt + "\n" + retry_reason}],
                json_mode=False, max_tokens=max(60, spec.narrator_max_tokens - 20), spec=spec,
                timeout=min(spec.narrator_timeout, time_left()), campaign_id=campaign_id,
                step_no=step_no, mask_terms=mask_terms,
            )
            retry_prose = _grounded_prose(_polish_prose(retry_text, fact), grounding)
            if retry_prose and (not recent_prose or not _too_similar(retry_prose, recent_prose[0])):
                prose = retry_prose
        if prose and own_text:
            # The resolved action is the canon core of the scene. The model may
            # add atmosphere, but it must never replace what actually happened.
            prose = _join_sentences(re.split(r"(?<=[.!?])\s+", own_text) + [prose])
    else:
        text = await provider.complete(
            "narrator",
            [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
            json_mode=True, max_tokens=500, spec=spec,
            timeout=spec.narrator_timeout, campaign_id=campaign_id, step_no=step_no,
            mask_terms=mask_terms,
        )
        parsed = extract_json(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("prose"), str) and parsed["prose"].strip():
            candidates = parsed.get("entities_used", [])
            raw_used = [str(item) for item in candidates] if isinstance(candidates, list) else []
            lowered = {token.lower() for token in allowed_tokens}
            # Keep only real entities from ALLOWED; never discard good prose because
            # the model added a descriptive adjective to entities_used.
            entities = [
                item for item in raw_used
                if item.lower() in lowered
                or any(item.lower().startswith(token[:4]) for token in lowered if len(token) >= 4)
            ]
            prose = _clean_prose(parsed["prose"], fact)
    if not prose.strip():
        if not local:
            # The free gateway often answers with an empty body after tens of
            # seconds. The local narrator is the reliable second voice.
            backup = models.get("fast")
            if backup and backup.provider == "ollama" and time_left() > 6:
                try:
                    backup_system = (NARRATOR_SYSTEM_LOCAL.replace("__STYLE__", style)
                                     .replace("__ALLOWED__", short_allowed)
                                     .replace("__LANGUAGE__", language))
                    local_text = await provider.complete(
                        "narrator",
                        [{"role": "system", "content": backup_system},
                         {"role": "user", "content": user_prompt}],
                        json_mode=False, max_tokens=backup.narrator_max_tokens, spec=backup,
                        timeout=min(30, time_left()),
                        campaign_id=campaign_id, step_no=step_no,
                    )
                    prose = _grounded_prose(_polish_prose(local_text, fact), grounding)
                except Exception:  # noqa: BLE001
                    prose = ""
        if not prose.strip():
            # Last resort: a readable canon-only scene from the engine facts.
            prose = _fallback_prose(results, world_facts, state, member, step_no)
        entities = []
    return uid, prose, entities


def _is_stale(timestamp: str | None, threshold: int) -> bool:
    """SQLite stores UTC as 'YYYY-MM-DD HH:MM:SS'."""
    if not timestamp:
        return True
    try:
        value = datetime.strptime(str(timestamp)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - value).total_seconds() >= threshold


async def _claim_resolution(campaign_id: str, expected_step_no: int | None = None,
                            resolution_id: str | None = None) -> dict[str, Any] | None:
    """Atomically reserve a filled step for resolution.

    The claim is committed on its own, so only one process can own a
    (campaign, step_no) pair. The unique resolution_id is the fencing token:
    a worker that loses the claim can never commit its result later. The lease
    only tells recovery when a claim may be released; it is not the authority.
    """
    token = str(resolution_id or uuid.uuid4().hex)
    expires = _deadline_after(settings.resolution_stale_seconds)
    async with db.transaction(immediate=True) as conn:
        cur = await conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
        row = await cur.fetchone()
        if not row:
            return None
        campaign = dict(row)
        step_no = int(campaign["step_no"])
        if expected_step_no is not None and int(expected_step_no) != step_no:
            return None
        if campaign["phase"] != "orders":
            return None
        if db.loads(campaign["pending_json"], []) != []:
            return None
        if campaign.get("resolution_id"):
            return None  # a previous claim must be reset before a new one
        cur = await conn.execute(
            "SELECT 1 FROM orders WHERE campaign_id=? AND step_no=? LIMIT 1",
            (campaign_id, step_no),
        )
        if await cur.fetchone() is None:
            return None  # never resolve an empty step
        cur = await conn.execute(
            """UPDATE campaigns SET phase='resolving', resolution_id=?,
                       resolution_expires_at=?, updated_at=datetime('now')
               WHERE id=? AND step_no=? AND phase='orders' AND pending_json='[]'
                 AND (resolution_id IS NULL OR resolution_id='')""",
            (token, expires, campaign_id, step_no),
        )
        if cur.rowcount != 1:
            return None
        campaign["resolution_id"] = token
        campaign["resolution_expires_at"] = expires
        return campaign


async def _reset_stale_claim(campaign_id: str, step_no: int,
                             resolution_id: str | None = None) -> bool:
    """Release a claim left behind by a crashed or timed-out worker.

    The release is keyed by the fencing token, so a late recovery cannot
    release a newer worker's claim. The lease is only a hint for when a claim
    may be considered abandoned.
    """
    async with campaign_lock(campaign_id):
        async with db.transaction(immediate=True) as conn:
            cur = await conn.execute(
                """SELECT resolution_id,resolution_expires_at,updated_at FROM campaigns
                   WHERE id=? AND phase='resolving' AND step_no=?""",
                (campaign_id, step_no),
            )
            row = await cur.fetchone()
            if not row:
                return False
            token = str(row["resolution_id"] or "")
            if resolution_id is not None and str(resolution_id) != token:
                return False
            expiry = str(row["resolution_expires_at"] or "")
            lease_expired = bool(expiry and expiry <= _utc_now())
            stale = lease_expired or _is_stale(
                row["updated_at"], settings.resolution_stale_seconds
            )
            if not stale:
                return False
            cur = await conn.execute(
                """UPDATE campaigns SET phase='orders', resolution_id='',
                           resolution_expires_at=NULL, updated_at=datetime('now')
                   WHERE id=? AND phase='resolving' AND step_no=? AND resolution_id=?""",
                (campaign_id, step_no, token),
            )
            return cur.rowcount == 1


async def expire_orders() -> None:
    """Auto-submit 'wait' for players whose order deadline has passed."""
    rows = await db.fetchall(
        """SELECT id,step_no FROM campaigns
           WHERE phase='orders' AND order_deadline_ts IS NOT NULL
           AND order_deadline_ts < datetime('now') AND pending_json != '[]'"""
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        campaign_id = row["id"]
        step_no = int(row["step_no"])
        stored: list[tuple[int, int, str]] = []
        async with campaign_lock(campaign_id):
            async with db.transaction(immediate=True) as conn:
                cur = await conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
                campaign = await cur.fetchone()
                if (not campaign or campaign["phase"] != "orders"
                        or int(campaign["step_no"]) != step_no
                        or not campaign["order_deadline_ts"]
                        or str(campaign["order_deadline_ts"]) >= now):
                    continue
                pending = db.loads(campaign["pending_json"], [])
                if not pending:
                    continue
                for uid in pending:
                    order = {
                        "action_id": "wait", "target": None, "method_tags": [],
                        "confidence": 0.6, "clarifying_question": None,
                        "raw_text": "(авто-приказ: время истекло)", "order_auto": True,
                    }
                    await conn.execute(
                        """INSERT OR REPLACE INTO orders(campaign_id,step_no,user_id,action_json,preview_json)
                           VALUES(?,?,?,?,?)""",
                        (campaign_id, step_no, uid, db.dumps(order), "{}"),
                    )
                    text = "Время на приказ истекло. Ты замер в нерешительности, пока мир шёл дальше."
                    seq = await _store_event(conn, campaign_id, uid, "order_timeout", {
                        "step_no": step_no, "prose": text,
                    })
                    stored.append((uid, seq, text))
                await conn.execute(
                    """UPDATE campaigns SET pending_json='[]',order_deadline_ts=NULL
                       WHERE id=? AND step_no=? AND phase='orders'""",
                    (campaign_id, step_no),
                )
            for uid, seq, text in stored:
                await manager.send(campaign_id, uid, "order_timeout", {
                    "step_no": step_no, "prose": text, "event_id": seq,
                })
        await _resolve_step(campaign_id, expected_step_no=step_no)


async def recover_stale_resolutions() -> None:
    """Startup recovery for steps interrupted by a server crash."""
    stale = await db.fetchall(
        """SELECT id,step_no,resolution_id,resolution_expires_at,updated_at
           FROM campaigns WHERE phase='resolving'"""
    )
    for row in stale:
        if await _reset_stale_claim(row["id"], int(row["step_no"]), row["resolution_id"]):
            await _resolve_step(row["id"], expected_step_no=int(row["step_no"]))

    # A crash between the last order and the resolve call leaves no claim at all.
    ready = await db.fetchall(
        "SELECT id,step_no FROM campaigns WHERE phase='orders' AND pending_json='[]'"
    )
    for row in ready:
        await _resolve_step(row["id"], expected_step_no=int(row["step_no"]))


async def _maybe_update_summary(state: dict[str, Any], step_no: int, prose: list[str],
                                spec: models.ModelSpec, campaign_id: str,
                                language: str) -> None:
    """Rolling memory so the narrator never starts from zero."""
    old = state.get("scene_summary") or {}
    old_location = str(old.get("location", ""))
    new_location = str((state.get("world", {}).get("location") or {}).get("id", ""))
    changed = bool(old_location) and old_location != new_location
    due = step_no % 3 == 0
    if not changed and not due and old.get("text"):
        return
    facts = [str(item).split(": ", 1)[-1]
             for item in (state.get("_journal_pending") or [])[-6:]]
    concrete = [item for item in facts
                if item and not item.lower().startswith(_GENERIC_ACTION_PREFIXES)]
    if concrete:
        facts = concrete
    if not facts:
        facts = [str(item) for item in prose[-2:]]
    if not facts:
        return
    summary_text = _join_sentences(facts)[:600]
    state["scene_summary"] = {
        "text": summary_text,
        "step_range": [old.get("step_range", [step_no, step_no])[0], step_no],
        "updated_at": datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
        "location": new_location,
    }


async def _resolve_step(campaign_id: str, expected_step_no: int | None = None,
                        resolution_id: str | None = None) -> None:
    """Resolve one claimed step under a cross-process lock.

    Only a campaign reserved by _claim_resolution is touched. The claim is
    short; the model is called outside any transaction. The whole result is
    committed in one final transaction guarded by step_no and resolution_id,
    so an old worker can never overwrite a newer claim.
    """
    async with campaign_lock(campaign_id):
        campaign = await _claim_resolution(campaign_id, expected_step_no, resolution_id)
        if not campaign:
            return
        token = str(campaign["resolution_id"] or "")
        step_no = int(campaign["step_no"])
        pack = load_pack(campaign["pack_id"])
        spec = models.get(campaign.get("model_id"))
        state = db.loads(campaign["state_json"], {})
        _ensure_state(pack, state)
        state["id"] = campaign_id
        order_rows = await db.fetchall(
            "SELECT * FROM orders WHERE campaign_id=? AND step_no=?", (campaign_id, step_no)
        )
        initiative = pack["rules"].get("initiative_attribute", "agi")
        order_rows.sort(key=lambda r: db.loads(r["action_json"], {}).get("confidence", 0), reverse=True)
        order_rows.sort(key=lambda r: next(
            (m["character"].get("attributes", {}).get(initiative, 0)
             for m in state["members"] if m["user_id"] == r["user_id"]), 0
        ), reverse=True)

        state_before = copy.deepcopy(state)
        results: list[dict[str, Any]] = []
        ui_events: list[str] = []
        for row in order_rows:
            user_id = row["user_id"]
            order = db.loads(row["action_json"], {})
            member = next((m for m in state["members"] if m["user_id"] == user_id), None)
            if not member:
                continue
            seed = f"{campaign_id}:{step_no}:{user_id}:{order.get('action_id')}:{order.get('raw_text','')}"
            result = resolve_action(pack, state, member, order, seed)
            if result["status"] == "resolved":
                facts = _apply_effects(pack, state, member, result.get("effects", {}), order,
                                       pack["rules"], seed)
                result["applied"] = facts
                if result.get("xp"):
                    member["character"]["xp"] = member["character"].get("xp", 0) + int(result["xp"])
                    _level_up(pack, member["character"])
                effect_ui = result.get("effects", {}).get("ui")
                if effect_ui:
                    ui_events.append(effect_ui)
            result["user_id"] = user_id
            result["name"] = member["character"].get("name")
            results.append(result)
            if result.get("status") == "resolved":
                state.setdefault("_journal_pending", []).append(
                    f"{member['character'].get('name')}: {result.get('text', '')}"
                )

        tick = _world_tick(pack, state, campaign_id, step_no)
        world_facts = list(tick.get("facts", []))

        allowed_parts = ([str(item.get("name", "")).replace("Выход: ", "")
                          for item in worldsim.nearby_for(pack, state, state["members"][0])]
                         if state.get("members") else [])
        living = state.get("living", {})
        weather_names = {
            "rain": "дождь", "clear": "ясно", "overcast": "пасмурно",
            "fog": "туман", "snow": "снег", "storm": "гроза",
        }
        time_names = {"day": "день", "morning": "утро", "evening": "вечер", "night": "ночь"}
        allowed_parts += [
            weather_names.get(str(living.get("weather", "")), str(living.get("weather", ""))),
            time_names.get(str(living.get("time_of_day", "")), str(living.get("time_of_day", ""))),
            str(state.get("refuge", {}).get("name", "")),
        ]
        allowed_parts += world_facts
        allowed_tokens = {str(part) for part in allowed_parts if part}
        allowed = ", ".join(part for part in dict.fromkeys(allowed_parts) if part)
        language = str(campaign.get("language") or "ru")

        active_members = [m for m in state["members"] if m.get("status", "active") == "active"]
        history_rows = await db.fetchall(
            """SELECT step_no,result_json FROM steps WHERE campaign_id=?
               ORDER BY step_no DESC LIMIT 1""",
            (campaign_id,),
        )
        recent_by_uid: dict[int, list[str]] = {m["user_id"]: [] for m in active_members}
        for row in history_rows:
            payload = db.loads(row["result_json"], {})
            for uid in recent_by_uid:
                text = (payload.get("narrations") or {}).get(str(uid), "")
                if text:
                    recent_by_uid[uid].append(text)
        for uid in recent_by_uid:
            recent_by_uid[uid].reverse()
        summary = str((state.get("scene_summary") or {}).get("text", ""))
        raw_by_uid = {
            row["user_id"]: str(db.loads(row["action_json"], {}).get("raw_text", ""))
            for row in order_rows
        }

        narration_pairs = await asyncio.gather(*[
            _narrate_one(
                m, state, pack, results, world_facts, allowed, allowed_tokens,
                spec, campaign_id, step_no,
                raw_text=raw_by_uid.get(m["user_id"], ""),
                recent_prose=recent_by_uid.get(m["user_id"], []),
                summary=summary, language=language,
            )
            for m in active_members
        ])
        narrations: dict[int, str] = {uid: text for uid, text, _ in narration_pairs}
        entities_used: dict[int, list[str]] = {uid: used for uid, _, used in narration_pairs}
        await _maybe_update_summary(state, step_no, list(narrations.values()), spec,
                                    campaign_id, language)

        next_step = step_no + 1
        pending = [m["user_id"] for m in state["members"]
                   if m["character"].get("alive", True) and m.get("status", "active") == "active"
                   and not m["character"].get("pending_class_selection")]
        random.Random(f"{campaign_id}:{step_no}:pending").shuffle(pending)
        state["step_no"] = next_step
        public_results = [
            {k: v for k, v in r.items()
             if k not in ("reason_code", "capability", "requirement", "margin",
                          "modifiers", "effects", "applied", "roll")}
            for r in results
        ]
        step_payload = {
            "results": public_results,
            "narrations": narrations,
            "entities_used": {str(uid): used for uid, used in entities_used.items()},
            "ui_events": ui_events,
            "world": {"facts": world_facts, "event": tick.get("event"), "era": state.get("era")},
            "state_before": state_before,
        }

        journal = _journal_lines(state.pop("_journal_pending", []), state.get("members", []))
        pending_names = await _user_names(pending)
        turn_payloads: list[tuple[int, dict[str, Any]]] = []
        for member in active_members:
            uid = member["user_id"]
            own_text = " ; ".join(r["text"] for r in results if r["user_id"] == uid)
            turn_payloads.append((uid, {
                "step_no": step_no,
                "prose": narrations.get(uid, ""),
                "anchor": anchor_view(state, member),
                "character": character_view(member),
                "presentation": presentation_view(state),
                "living": _living_view(pack, state),
                "outcome_text": own_text,
                "ui_events": ui_events,
                "next_step": next_step,
                "waiting_for": pending_names,
                "my_turn": uid in pending,
                "class_selection": _class_selection_view(pack, state, member),
            }))
        orders_payload = {"step_no": next_step, "pending": pending_names}
        resolved_payload = {
            "step": step_no, "next_step": next_step,
            "pending": pending_names, "results_ready": True,
        }
        class_payloads: list[tuple[int, dict[str, Any]]] = []
        for member in active_members:
            if member["character"].get("pending_class_selection"):
                class_payloads.append((member["user_id"],
                                       _class_selection_view(pack, state, member)))

        stored: list[tuple[int | None, str, dict[str, Any], int]] = []
        try:
            async with db.transaction(immediate=True) as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO steps(campaign_id,step_no,result_json) VALUES(?,?,?)",
                    (campaign_id, step_no, db.dumps(step_payload)),
                )
                for text in journal:
                    if text:
                        await conn.execute(
                            "INSERT INTO ledger(campaign_id,text,source,status,step_no) VALUES(?,?,?,?,?)",
                            (campaign_id, str(text), "system", "canon", step_no),
                        )
                for entry in state.get("members", []):
                    await conn.execute(
                        "UPDATE campaign_members SET character_json=? WHERE campaign_id=? AND user_id=?",
                        (db.dumps(entry["character"]), campaign_id, entry["user_id"]),
                    )
                # Notifications are part of the same commit as the game state.
                for uid, payload in turn_payloads:
                    stored.append((uid, "turn_final", payload,
                                   await _store_event(conn, campaign_id, uid, "turn_final", payload)))
                stored.append((None, "orders_open", orders_payload,
                               await _store_event(conn, campaign_id, None, "orders_open", orders_payload)))
                stored.append((None, "step_resolved", resolved_payload,
                               await _store_event(conn, campaign_id, None, "step_resolved", resolved_payload)))
                for uid, payload in class_payloads:
                    stored.append((uid, "class_selection", payload,
                                   await _store_event(conn, campaign_id, uid, "class_selection", payload)))
                # Final compare-and-set: the step must still be the one we
                # claimed, and the fencing token must still be ours.
                cur = await conn.execute(
                    """UPDATE campaigns SET state_json=?,step_no=?,pending_json=?,phase='orders',
                       order_deadline_ts=?,resolution_id='',resolution_expires_at=NULL,
                       updated_at=datetime('now')
                       WHERE id=? AND phase='resolving' AND step_no=? AND resolution_id=?""",
                    (db.dumps(state), next_step, db.dumps(pending),
                     _deadline_after(settings.order_deadline_seconds),
                     campaign_id, step_no, token),
                )
                if cur.rowcount != 1:
                    raise ResolutionConflict()
        except ResolutionConflict:
            log.warning("resolution of %s step %s lost the claim", campaign_id, step_no)
            return

        for uid, event, payload, seq in stored:
            message = dict(payload)
            message["event_id"] = seq
            if uid is None:
                await manager.broadcast(campaign_id, event, message)
            else:
                await manager.send(campaign_id, uid, event, message)


def _living_view(pack: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    living = state.get("living", {})
    return {
        "era": state.get("era", "E0"),
        "era_name": ((catalog.eras(pack).get("eras", {}) or {}).get(state.get("era", "E0"), {}) or {}).get("name", ""),
        "time": state.get("world", {}).get("time", {}),
        "weather": living.get("weather", ""),
        "temperature_c": living.get("temperature_c", 0),
        "season": living.get("season", ""),
        "time_of_day": living.get("time_of_day", ""),
        "events": living.get("events", [])[-6:],
        "rumors": living.get("rumors", [])[-5:],
        "factions": [faction_view(item) for item in state.get("factions", {}).values()],
        "npcs": [npc_view(item) for item in state.get("npcs", {}).values() if item.get("alive", True)],
        "refuge": refuge_view(state.get("refuge", {})),
    }


def _class_selection_view(pack: dict[str, Any], state: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    character = member.get("character", {})
    pending = bool(character.get("pending_class_selection"))
    offers = []
    if pending:
        offers = [catalog.class_view(entry) for entry in catalog.available_classes(pack, state, member)]
    return {
        "pending": pending,
        "offers": offers,
        "selected": {
            "id": character.get("class_id"),
            "name": character.get("class_name"),
            "category": character.get("class_category"),
        } if character.get("class_id") else None,
        "era": state.get("era", "E0"),
    }


async def class_offers(campaign_id: str, user_id: int) -> dict[str, Any]:
    ctx = await get_context(campaign_id, user_id)
    if not ctx:
        return {"error": "NOT_IN_CAMPAIGN"}
    campaign, state, member, _ = ctx
    pack = load_pack(campaign["pack_id"])
    return _class_selection_view(pack, state, member)


async def choose_class(campaign_id: str, user_id: int, class_id: str) -> dict[str, Any]:
    async with campaign_lock(campaign_id):
        ctx = await get_context(campaign_id, user_id)
        if not ctx:
            return {"error": "NOT_IN_CAMPAIGN"}
        campaign, state, member, _ = ctx
        character = member["character"]
        pack = load_pack(campaign["pack_id"])
        if not character.get("pending_class_selection"):
            return {"error": "NOT_PENDING", "message": "Сейчас класс не выбирают."}
        entry = next((c for c in catalog.available_classes(pack, state, member) if c.get("id") == class_id), None)
        if not entry:
            return {"error": "CLASS_LOCKED", "message": "Этот класс пока недоступен."}

        for key, delta in (entry.get("attributes") or {}).items():
            character.setdefault("attributes", {})
            character["attributes"][key] = int(character["attributes"].get(key, 10)) + int(delta)
        for skill in entry.get("skills", []):
            if skill.get("id") not in [s.get("id") for s in character.get("skills", [])]:
                character.setdefault("skills", []).append(dict(skill))
        for gear in entry.get("gear", []):
            _add_item(character, pack, str(gear.get("item")), int(gear.get("qty", 1) or 1))
        for slot, item_id in (entry.get("equipment") or {}).items():
            character.setdefault("equipment", {})[slot] = item_id
        character["perks"] = list(entry.get("perks", []))
        if entry.get("starting_coins"):
            currency = character.setdefault("currency", {})
            currency["coins"] = int(currency.get("coins", 0)) + int(entry["starting_coins"])
        character["class_id"] = entry.get("id")
        character["class_name"] = entry.get("name")
        character["class_category"] = entry.get("category")
        character["pending_class_selection"] = False
        character["pending_level_up"] = bool(character.get("unspent_points", 0))
        state.setdefault("flags", {})["class_chosen"] = True
        state.setdefault("_journal_pending", []).append(
            f"Выбран класс «{entry.get('name')}». Система фиксирует путь."
        )
        _normalize_character(pack, character)
        for row in state.get("members", []):
            if row["user_id"] == user_id:
                row["character"] = character
        pending = db.loads(campaign["pending_json"], [])
        if campaign["phase"] == "orders" and user_id not in pending:
            pending.append(user_id)
        await _flush_journal(campaign_id, state, campaign["step_no"])
        await _save_state(campaign_id, state, campaign["step_no"], pending, campaign["phase"])
        await manager.send(campaign_id, user_id, "character_updated", {"character": character_view(member)})
        await manager.send(campaign_id, user_id, "class_chosen", {
            "class": catalog.class_view(entry), "character": character_view(member),
        })
        await emit(campaign_id, None, "orders_open", {
            "step_no": campaign["step_no"], "pending": await _user_names(pending),
        })
        return {"ok": True, "class": catalog.class_view(entry), "character": character_view(member)}


async def set_campaign_model(campaign_id: str, user_id: int, model_id: str) -> dict[str, Any]:
    async with campaign_lock(campaign_id):
        ctx = await get_context(campaign_id, user_id)
        if not ctx:
            return {"error": "NOT_IN_CAMPAIGN"}
        campaign, state, member, _ = ctx
        if not member.get("is_founder"):
            return {"error": "NOT_FOUNDER", "message": "Модель меняет основатель игры."}
        if campaign["phase"] != "orders":
            return {"error": "STEP_IN_PROGRESS", "message": "Модель можно менять между ходами."}
        if not models.validate(model_id):
            return {"error": "MODEL_UNAVAILABLE", "message": "Такая модель недоступна."}
        await db.execute(
            "UPDATE campaigns SET model_id=?,updated_at=datetime('now') WHERE id=?",
            (model_id, campaign_id),
        )
        await _audit(user_id, "model_changed", {"campaign_id": campaign_id, "model_id": model_id})
        return {"ok": True, "model": models.get(model_id).view()}


async def delete_campaign(campaign_id: str, user_id: int) -> dict[str, Any]:
    """Remove a campaign from the player's account.

    If the player is the only active member the campaign is deleted entirely;
    otherwise it is only removed from this player's list.
    """
    name = (await _user_names([user_id]))[0]
    removed = False
    async with mutate(campaign_id, user_id) as m:
        if m is None or m.member is None or m.member["status"] != "active":
            return {"error": "NOT_IN_CAMPAIGN"}
        row = await db.fetchone(
            """SELECT COUNT(*) AS n FROM campaign_members
               WHERE campaign_id=? AND status='active'""",
            (campaign_id,),
        )
        if (row and int(row["n"]) <= 1):
            await m.conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
            removed = True
        else:
            await m.conn.execute(
                """UPDATE campaign_members SET status='left',left_at=datetime('now')
                   WHERE campaign_id=? AND user_id=?""",
                (campaign_id, user_id),
            )
            for entry in m.state.get("members", []):
                if entry["user_id"] == user_id:
                    entry["status"] = "left"
            m.pending = [p for p in m.pending if p != user_id]
    await _audit(user_id, "campaign_deleted", {"campaign_id": campaign_id})
    if not removed:
        campaign = await db.fetchone(
            "SELECT pending_json FROM campaigns WHERE id=?", (campaign_id,)
        )
        pending_ids = db.loads(campaign["pending_json"], []) if campaign else []
        await emit(campaign_id, None, "member_left", {
            "name": name, "pending": await _user_names(pending_ids),
        })
    return {"ok": True, "campaign_id": campaign_id}
