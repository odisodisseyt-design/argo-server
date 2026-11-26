from server import db, engine
from server.provider import LLMProvider, provider


async def test_deadline_is_set_and_expiry_auto_submits_wait(player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    row = await db.fetchone(
        "SELECT order_deadline_ts FROM campaigns WHERE id=?", (campaign_id,)
    )
    assert row["order_deadline_ts"]

    await db.execute(
        "UPDATE campaigns SET order_deadline_ts=datetime('now','-1 hour') WHERE id=?",
        (campaign_id,),
    )
    await engine.expire_orders()

    orders = await db.fetchall(
        "SELECT action_json FROM orders WHERE campaign_id=? AND step_no=1", (campaign_id,)
    )
    assert len(orders) == 1
    assert db.loads(orders[0]["action_json"], {})["order_auto"] is True
    event = await db.fetchone(
        "SELECT event FROM campaign_events WHERE campaign_id=? AND event='order_timeout'",
        (campaign_id,),
    )
    assert event is not None
    campaign = await db.fetchone(
        "SELECT step_no,phase FROM campaigns WHERE id=?", (campaign_id,)
    )
    assert campaign["step_no"] == 2
    assert campaign["phase"] == "orders"


async def test_step_status_exposes_deadline(client, player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    response = await client.get(
        f"/api/campaign/{campaign_id}/step/status", headers=player["headers"]
    )
    assert response.status_code == 200
    assert response.json()["deadline"]


async def test_invalid_entities_fall_back_and_are_audited(player, monkeypatch):
    async def bad_narrator(purpose, messages, *args, **kwargs):
        if purpose == "intent":
            return '{"action_id":"","confidence":0}'
        return '{"prose":"Текст с выдумкой.","entities_used":["неизвестный_ид"]}'

    monkeypatch.setattr("server.provider.provider.complete", bad_narrator)
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    await engine.submit_order(campaign_id, player["id"], "Осмотреться")
    row = await db.fetchone(
        "SELECT result_json FROM steps WHERE campaign_id=?", (campaign_id,)
    )
    payload = db.loads(row["result_json"], {})
    assert payload["entities_used"][str(player["id"])] == []
    assert payload["narrations"][str(player["id"])]
    audit = await db.fetchone(
        "SELECT * FROM audit_logs WHERE action='invalid_entities' LIMIT 1"
    )
    assert audit is None


async def test_class_selection_uses_single_source():
    state = {
        "members": [
            {"user_id": 1, "status": "active",
             "character": {"alive": True, "pending_class_selection": False}},
        ],
        "flags": {}, "_journal_pending": [],
    }
    engine._begin_class_selection(state)
    assert state["members"][0]["character"]["pending_class_selection"] is True
    assert "pending_users" not in state.get("class_selection", {})


async def test_llm_logs_mask_emails_and_names(monkeypatch):
    async def fake_ollama(*args, **kwargs):
        return "Привет, player_test, пиши на mail@example.com"

    monkeypatch.setattr(provider, "_ollama", fake_ollama)
    real_complete = LLMProvider.complete.__get__(provider)
    await real_complete(
        "narrator",
        [{"role": "user", "content": "Игрок player_test, email mail@example.com"}],
        json_mode=False,
        mask_terms=("player_test",),
    )
    row = await db.fetchone("SELECT prompt,response FROM llm_logs ORDER BY id DESC LIMIT 1")
    assert "mail@example.com" not in row["prompt"]
    assert "player_test" not in row["prompt"]
    assert "[email]" in row["response"]
    assert "[имя]" in row["response"]
