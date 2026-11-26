"""Fencing token for step resolution: one claim, one commit."""
from contextlib import asynccontextmanager

import pytest

from server import db, engine


async def _ready_campaign(player, step_no: int = 1) -> str:
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    order = {
        "action_id": "look", "target": None, "method_tags": [],
        "confidence": 0.95, "clarifying_question": None, "raw_text": "Осмотреться",
    }
    await db.execute(
        """INSERT INTO orders(campaign_id,step_no,user_id,action_json,preview_json)
           VALUES(?,?,?,?,?)""",
        (campaign_id, step_no, player["id"], db.dumps(order), "{}"),
    )
    await db.execute(
        "UPDATE campaigns SET pending_json='[]',step_no=? WHERE id=?",
        (step_no, campaign_id),
    )
    return campaign_id


async def _campaign_row(campaign_id: str) -> dict:
    return await db.fetchone(
        "SELECT step_no,phase,resolution_id,resolution_expires_at FROM campaigns WHERE id=?",
        (campaign_id,),
    )


async def test_claim_creates_and_stores_unique_token(player):
    campaign_id = await _ready_campaign(player)
    claim = await engine._claim_resolution(campaign_id, 1)
    assert claim is not None
    token = str(claim["resolution_id"])
    assert len(token) >= 16

    row = await _campaign_row(campaign_id)
    assert row["phase"] == "resolving"
    assert row["resolution_id"] == token
    assert row["resolution_expires_at"]
    assert await engine._claim_resolution(campaign_id, 1) is None


async def test_old_worker_cannot_commit_after_reclaim(player, monkeypatch):
    campaign_id = await _ready_campaign(player)
    first = await engine._claim_resolution(campaign_id, 1)
    old_token = str(first["resolution_id"])

    # The lease expires; recovery releases the old claim and a new worker
    # acquires the same step with a different token.
    await db.execute(
        "UPDATE campaigns SET resolution_expires_at=datetime('now','-1 second') WHERE id=?",
        (campaign_id,),
    )
    assert await engine._reset_stale_claim(campaign_id, 1, old_token) is True
    second = await engine._claim_resolution(campaign_id, 1)
    assert second is not None
    new_token = str(second["resolution_id"])
    assert new_token != old_token

    # The old worker still thinks it owns the step and tries to finish it.
    stale_view = dict(second)
    stale_view["resolution_id"] = old_token
    async def stale_claim(*args, **kwargs):
        return stale_view
    monkeypatch.setattr(engine, "_claim_resolution", stale_claim)
    await engine._resolve_step(campaign_id, expected_step_no=1, resolution_id=old_token)

    row = await _campaign_row(campaign_id)
    steps = await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))
    assert row["phase"] == "resolving"
    assert row["resolution_id"] == new_token
    assert row["step_no"] == 1
    assert steps == []


async def test_second_commit_of_same_resolution_is_blocked(player, monkeypatch):
    campaign_id = await _ready_campaign(player)
    await engine._resolve_step(campaign_id, expected_step_no=1)
    row = await _campaign_row(campaign_id)
    assert row["step_no"] == 2
    assert row["resolution_id"] == ""
    assert len(await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))) == 1

    # A stale worker replays the old claim against the already committed step.
    stale_view = await db.fetchone("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
    stale_view = dict(stale_view)
    stale_view["step_no"] = 1
    stale_view["resolution_id"] = "old-token"
    async def stale_claim(*args, **kwargs):
        return stale_view
    monkeypatch.setattr(engine, "_claim_resolution", stale_claim)
    await engine._resolve_step(campaign_id, expected_step_no=1, resolution_id="old-token")

    after = await _campaign_row(campaign_id)
    assert after["step_no"] == 2
    assert len(await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))) == 1


async def test_recovery_replays_crashed_claim_with_token(player, monkeypatch):
    campaign_id = await _ready_campaign(player)
    real_transaction = db.transaction
    calls = {"n": 0}

    @asynccontextmanager
    async def flaky_transaction(immediate=False):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash")
        async with real_transaction(immediate=immediate) as conn:
            yield conn

    monkeypatch.setattr(engine.db, "transaction", flaky_transaction)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await engine._resolve_step(campaign_id, expected_step_no=1)

    claimed = await _campaign_row(campaign_id)
    assert claimed["phase"] == "resolving"
    assert claimed["resolution_id"]

    monkeypatch.setattr(engine.db, "transaction", real_transaction)
    await db.execute(
        "UPDATE campaigns SET resolution_expires_at=datetime('now','-1 second') WHERE id=?",
        (campaign_id,),
    )
    await engine.recover_stale_resolutions()

    row = await _campaign_row(campaign_id)
    assert row["step_no"] == 2
    assert row["phase"] == "orders"
    assert row["resolution_id"] == ""
    assert len(await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))) == 1


async def test_two_workers_cannot_apply_one_step_twice(player):
    campaign_id = await _ready_campaign(player)
    await engine._resolve_step(campaign_id, expected_step_no=1)
    await engine._resolve_step(campaign_id, expected_step_no=1)
    row = await _campaign_row(campaign_id)
    assert row["step_no"] == 2
    assert len(await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))) == 1
