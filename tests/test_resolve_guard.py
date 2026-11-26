import asyncio
from contextlib import asynccontextmanager

import pytest

from server import db, engine
from server.locks import LockBusy


async def _ready_campaign(player, step_no: int = 1) -> str:
    """Campaign with one order submitted and an empty pending list."""
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


async def test_concurrent_resolution_applies_once(player, monkeypatch):
    campaign_id = await _ready_campaign(player)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_narrate = engine._narrate_one

    async def slow_narrate(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_narrate(*args, **kwargs)

    monkeypatch.setattr(engine, "_narrate_one", slow_narrate)

    first = asyncio.create_task(engine._resolve_step(campaign_id, expected_step_no=1))
    await asyncio.wait_for(entered.wait(), timeout=3)
    second = asyncio.create_task(engine._resolve_step(campaign_id, expected_step_no=1))
    await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    steps = await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))
    ledger = await db.fetchall("SELECT * FROM ledger WHERE campaign_id=?", (campaign_id,))
    campaign = await db.fetchone(
        "SELECT step_no,phase FROM campaigns WHERE id=?", (campaign_id,)
    )
    assert len(steps) == 1
    assert len(ledger) == 1
    assert campaign["step_no"] == 2
    assert campaign["phase"] == "orders"
    assert any(isinstance(outcome, LockBusy) for outcome in outcomes)


async def test_empty_step_is_not_resolved(player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    await db.execute(
        "UPDATE campaigns SET pending_json='[]' WHERE id=?", (campaign_id,)
    )
    await engine._resolve_step(campaign_id, expected_step_no=1)
    campaign = await db.fetchone(
        "SELECT step_no,phase FROM campaigns WHERE id=?", (campaign_id,)
    )
    steps = await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))
    assert campaign["step_no"] == 1
    assert campaign["phase"] == "orders"
    assert steps == []


async def test_expected_step_mismatch_is_rejected(player):
    campaign_id = await _ready_campaign(player)
    await engine._resolve_step(campaign_id, expected_step_no=2)
    campaign = await db.fetchone(
        "SELECT step_no,phase FROM campaigns WHERE id=?", (campaign_id,)
    )
    steps = await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))
    assert campaign["step_no"] == 1
    assert campaign["phase"] == "orders"
    assert steps == []


async def test_stale_claim_is_recovered_once(player):
    campaign_id = await _ready_campaign(player)
    await db.execute(
        """UPDATE campaigns SET phase='resolving',
           updated_at=datetime('now','-600 seconds') WHERE id=?""",
        (campaign_id,),
    )
    await engine.recover_stale_resolutions()
    campaign = await db.fetchone(
        "SELECT step_no,phase FROM campaigns WHERE id=?", (campaign_id,)
    )
    steps = await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))
    ledger = await db.fetchall("SELECT * FROM ledger WHERE campaign_id=?", (campaign_id,))
    assert campaign["step_no"] == 2
    assert campaign["phase"] == "orders"
    assert len(steps) == 1
    assert len(ledger) == 1
    events = await db.fetchall(
        "SELECT event FROM campaign_events WHERE campaign_id=?", (campaign_id,)
    )
    assert [event["event"] for event in events].count("turn_final") == 1
    assert [event["event"] for event in events].count("step_resolved") == 1


async def test_unclaimed_ready_step_is_recovered(player):
    campaign_id = await _ready_campaign(player)
    await engine.recover_stale_resolutions()
    campaign = await db.fetchone(
        "SELECT step_no,phase FROM campaigns WHERE id=?", (campaign_id,)
    )
    steps = await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))
    assert campaign["step_no"] == 2
    assert campaign["phase"] == "orders"
    assert len(steps) == 1


async def test_crash_before_final_commit_is_replayed_without_duplicates(player, monkeypatch):
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

    campaign = await db.fetchone(
        "SELECT step_no,phase FROM campaigns WHERE id=?", (campaign_id,)
    )
    steps = await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))
    ledger = await db.fetchall("SELECT * FROM ledger WHERE campaign_id=?", (campaign_id,))
    assert campaign["step_no"] == 1
    assert campaign["phase"] == "resolving"
    assert steps == []
    assert ledger == []

    monkeypatch.setattr(engine.db, "transaction", real_transaction)
    await db.execute(
        "UPDATE campaigns SET updated_at=datetime('now','-600 seconds') WHERE id=?",
        (campaign_id,),
    )
    await engine.recover_stale_resolutions()

    campaign = await db.fetchone(
        "SELECT step_no,phase FROM campaigns WHERE id=?", (campaign_id,)
    )
    steps = await db.fetchall("SELECT * FROM steps WHERE campaign_id=?", (campaign_id,))
    ledger = await db.fetchall("SELECT * FROM ledger WHERE campaign_id=?", (campaign_id,))
    assert campaign["step_no"] == 2
    assert campaign["phase"] == "orders"
    assert len(steps) == 1
    assert len(ledger) == 1
