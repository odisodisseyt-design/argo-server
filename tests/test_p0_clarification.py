"""Clarification state survives restarts and never becomes a second order."""
import pytest

from server import db, engine
from server.provider import provider as provider_instance


async def _campaign(player) -> str:
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    await db.execute(
        "UPDATE campaigns SET pending_json=? WHERE id=?",
        (f'[{player["id"]}]', campaign_id),
    )
    return campaign_id


def _vague_llm(monkeypatch, confidence=0.3):
    async def complete(purpose, messages, *args, **kwargs):
        if purpose == "intent":
            return ('{"action_id":"","confidence":%s,'
                    '"clarifying_question":"Что именно ты хочешь сделать?"}' % confidence)
        return "Он осмотрелся вокруг."
    monkeypatch.setattr(provider_instance, "complete", complete)


def _clear_llm(monkeypatch):
    async def complete(purpose, messages, *args, **kwargs):
        if purpose == "intent":
            return '{"action_id":"look","target":null,"confidence":0.9}'
        return "Он осмотрелся вокруг."
    monkeypatch.setattr(provider_instance, "complete", complete)


async def _clarification(campaign_id: str):
    return await db.fetchone(
        "SELECT * FROM order_clarifications WHERE campaign_id=?", (campaign_id,)
    )


async def test_clarification_is_saved_with_original_text(player, monkeypatch):
    campaign_id = await _campaign(player)
    _vague_llm(monkeypatch)
    result = await engine.submit_order(campaign_id, player["id"], "Странное действие")
    assert result["order_saved"] is False
    assert result["clarifying_question"]
    assert result["clarification_id"]

    row = await _clarification(campaign_id)
    assert row is not None
    assert row["original_text"] == "Странное действие"
    assert row["question"] == result["clarifying_question"]
    assert row["status"] == "pending"
    assert row["step_no"] == 1
    assert row["created_at"] and row["expires_at"]


async def test_clarification_survives_reinitialisation(player, monkeypatch):
    campaign_id = await _campaign(player)
    _vague_llm(monkeypatch)
    first = await engine.submit_order(campaign_id, player["id"], "Странное действие")
    await db.init_db()  # same effect as a restart: migrations re-run
    row = await engine._load_clarification(
        campaign_id, player["id"], 1, first["clarification_id"]
    )
    assert row is not None
    assert row["original_text"] == "Странное действие"


async def test_answer_continues_original_order_and_is_idempotent(player, monkeypatch):
    campaign_id = await _campaign(player)
    _vague_llm(monkeypatch)
    first = await engine.submit_order(campaign_id, player["id"], "Странное действие")
    cid = first["clarification_id"]

    _clear_llm(monkeypatch)
    answered = await engine.submit_order(
        campaign_id, player["id"], "Осмотреться", clarification_id=cid,
        defer_resolution=True,
    )
    assert answered["order_saved"] is True

    orders = await db.fetchall("SELECT * FROM orders WHERE campaign_id=?", (campaign_id,))
    assert len(orders) == 1
    assert "Странное действие" in orders[0]["action_json"]
    assert "Осмотреться" in orders[0]["action_json"]

    repeated = await engine.submit_order(
        campaign_id, player["id"], "Совсем другое", clarification_id=cid
    )
    assert repeated["order_saved"] is True
    assert repeated["step_no"] == answered["step_no"]
    assert len(await db.fetchall("SELECT * FROM orders WHERE campaign_id=?", (campaign_id,))) == 1

    row = await _clarification(campaign_id)
    assert row["status"] == "answered"
    assert row["answer_response_json"]


async def test_stale_clarification_cannot_change_finished_step(player, monkeypatch):
    campaign_id = await _campaign(player)
    _vague_llm(monkeypatch)
    first = await engine.submit_order(campaign_id, player["id"], "Странное действие")
    _clear_llm(monkeypatch)
    await engine.submit_order(
        campaign_id, player["id"], "Осмотреться", clarification_id=first["clarification_id"],
        defer_resolution=False,
    )
    campaign = await db.fetchone("SELECT step_no FROM campaigns WHERE id=?", (campaign_id,))
    assert campaign["step_no"] == 2

    later = await engine.submit_order(
        campaign_id, player["id"], "Осмотреться", clarification_id=first["clarification_id"]
    )
    assert later["error"] == "CLARIFICATION_EXPIRED"
    assert len(await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))) == 1


async def test_expired_clarification_is_rejected(player, monkeypatch):
    campaign_id = await _campaign(player)
    _vague_llm(monkeypatch)
    first = await engine.submit_order(campaign_id, player["id"], "Странное действие")
    await db.execute(
        "UPDATE order_clarifications SET expires_at=datetime('now','-1 second') WHERE id=?",
        (first["clarification_id"],),
    )
    _clear_llm(monkeypatch)
    result = await engine.submit_order(
        campaign_id, player["id"], "Осмотреться", clarification_id=first["clarification_id"]
    )
    assert result["error"] == "CLARIFICATION_EXPIRED"
    assert (await _clarification(campaign_id))["status"] == "expired"


async def test_cancelled_clarification_is_rejected(player, monkeypatch):
    campaign_id = await _campaign(player)
    _vague_llm(monkeypatch)
    first = await engine.submit_order(campaign_id, player["id"], "Странное действие")
    cancelled = await engine.cancel_clarification(
        campaign_id, player["id"], first["clarification_id"]
    )
    assert cancelled["order_saved"] is False
    assert (await _clarification(campaign_id))["status"] == "answered"

    _clear_llm(monkeypatch)
    result = await engine.submit_order(
        campaign_id, player["id"], "Осмотреться", clarification_id=first["clarification_id"]
    )
    assert result == cancelled
    assert await db.fetchall("SELECT * FROM orders WHERE campaign_id=?", (campaign_id,)) == []


async def test_clarification_from_previous_step_is_rejected(player, monkeypatch):
    campaign_id = await _campaign(player)
    _vague_llm(monkeypatch)
    first = await engine.submit_order(campaign_id, player["id"], "Странное действие")
    await db.execute("UPDATE campaigns SET step_no=2 WHERE id=?", (campaign_id,))

    _clear_llm(monkeypatch)
    result = await engine.submit_order(
        campaign_id, player["id"], "Осмотреться", clarification_id=first["clarification_id"]
    )
    assert result["error"] == "CLARIFICATION_EXPIRED"
    assert await db.fetchall("SELECT * FROM orders WHERE campaign_id=?", (campaign_id,)) == []


async def test_http_turn_accepts_clarification_id(player, client, monkeypatch):
    campaign_id = await _campaign(player)
    _vague_llm(monkeypatch)
    response = await client.post("/api/turn", headers=player["headers"], json={
        "campaign_id": campaign_id, "command": "Странное действие",
    })
    assert response.status_code == 200
    cid = response.json()["clarification_id"]

    cancel = await client.post("/api/turn", headers=player["headers"], json={
        "campaign_id": campaign_id, "command": "", "clarification_id": cid,
    })
    assert cancel.status_code == 200
    assert cancel.json()["order_saved"] is False
    assert (await _clarification(campaign_id))["status"] == "answered"
