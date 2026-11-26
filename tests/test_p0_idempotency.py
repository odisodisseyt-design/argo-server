from server import db, engine


async def _campaign(player):
    return await engine.create_campaign("refuge", "Тест", player, [])


async def test_same_key_returns_saved_response_once(player):
    cid = await _campaign(player)
    first = await engine.submit_order(cid, player["id"], "Осмотреться", "key-1")
    second = await engine.submit_order(cid, player["id"], "Осмотреться", "key-1")
    assert first == second
    assert first["order_saved"] is True
    rows = await db.fetchall(
        "SELECT * FROM orders WHERE campaign_id=?", (cid,)
    )
    assert len(rows) == 1


async def test_key_is_scoped_to_owner(player, second_player):
    cid = await _campaign(player)
    await engine.submit_order(cid, player["id"], "Осмотреться", "key-2")
    result = await engine.submit_order(cid, second_player["id"], "Осмотреться", "key-2")
    assert result["error"] == "IDEMPOTENCY_MISMATCH"


async def test_expired_key_is_ignored(player):
    cid = await _campaign(player)
    await engine.submit_order(cid, player["id"], "Осмотреться", "key-3")
    await db.execute(
        "UPDATE turn_idempotency SET created_at=datetime('now','-25 hours') WHERE key='key-3'"
    )
    campaign = await db.fetchone("SELECT step_no FROM campaigns WHERE id=?", (cid,))
    await db.execute(
        "UPDATE campaigns SET phase='orders', pending_json=? WHERE id=?",
        (f'[{player["id"]}]', cid),
    )
    result = await engine.submit_order(cid, player["id"], "Осмотреться", "key-3")
    assert result["order_saved"] is True
    assert campaign is not None
