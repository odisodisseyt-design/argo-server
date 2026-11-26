import asyncio

import httpx

from server import db, engine
from server.main import app, buy


async def _campaign_with_order(player, text="Осмотреться"):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    response = await engine.submit_order(campaign_id, player["id"], text)
    return campaign_id, response


async def test_preview_hides_engine_internals(client, player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    response = await client.post(
        "/api/turn", headers=player["headers"],
        json={"campaign_id": campaign_id, "command": "Осмотреться"},
    )
    assert response.status_code == 200
    preview = response.json()["preview"]
    assert "margin" not in preview
    assert "modifiers" not in preview
    assert "capability" not in preview
    assert "chance" in preview


async def test_campaign_state_uses_names_and_my_turn(client, player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    response = await client.get(f"/api/campaign/{campaign_id}", headers=player["headers"])
    payload = response.json()
    assert payload["waiting_for"] == [player["username"]]
    assert payload["my_turn"] is True
    assert all(isinstance(name, str) for name in payload["waiting_for"])
    for entry in payload["journal"]:
        assert "source" not in entry
    for key in ("memory", "needs", "rep", "power"):
        assert key not in str(payload["living"])
    assert "knowledge" in payload


async def test_step_status_endpoint(client, player):
    campaign_id, _ = await _campaign_with_order(player)
    response = await client.get(
        f"/api/campaign/{campaign_id}/step/status", headers=player["headers"]
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["campaignId"] == campaign_id
    assert payload["step"] == 2
    assert payload["phase"] == "orders"
    assert payload["results_ready"] is True
    assert payload["participants"][0]["name"] == player["username"]
    assert payload["participants"][0]["isYou"] is True


async def test_steps_history_is_personal(client, player, second_player, monkeypatch):
    async def narrator(purpose, messages, *args, **kwargs):
        if purpose == "intent":
            return '{"action_id":"","confidence":0}'
        prompt = messages[-1]["content"]
        prose = ("Он осмотрелся вокруг."
                 if "Осмотреться" in prompt else "Он подождал.")
        return '{"prose":"%s","entities_used":[]}' % prose

    monkeypatch.setattr("server.provider.provider.complete", narrator)
    campaign_id = await engine.create_campaign("refuge", "Кооп", player, [second_player])
    await engine.submit_order(campaign_id, player["id"], "Осмотреться")
    await engine.submit_order(campaign_id, second_player["id"], "Подождать")

    first = await client.get(
        f"/api/campaign/{campaign_id}/steps", headers=player["headers"]
    )
    second = await client.get(
        f"/api/campaign/{campaign_id}/steps", headers=second_player["headers"]
    )
    assert first.json()["steps"][0]["prose"] != second.json()["steps"][0]["prose"]


async def test_events_are_replayable_and_contain_no_internal_ids(client, player, second_player):
    campaign_id = await engine.create_campaign("refuge", "Кооп", player, [second_player])
    await engine.submit_order(campaign_id, player["id"], "Осмотреться")
    await engine.submit_order(campaign_id, second_player["id"], "Подождать")

    response = await client.get(
        f"/api/campaign/{campaign_id}/events?after=0", headers=player["headers"]
    )
    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["event"] for event in events]
    assert any(event["event"] == "step_resolved" for event in events)
    assert any(event["event"] == "turn_final" for event in events)
    for event in events:
        payload = event["payload"]
        assert "user_id" not in payload
        assert "reason_code" not in payload
        assert "margin" not in payload
        for key in ("pending", "waiting_for"):
            if key in payload:
                assert all(isinstance(item, str) for item in payload[key])


async def test_concurrent_buy_oversell_is_impossible(player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    async with engine.mutate(campaign_id, player["id"]) as m:
        m.member["character"]["currency"]["coins"] = 1000
        m.state.setdefault("shop_stock", {})["bandage"] = 1

    outcomes = await asyncio.gather(
        buy({"campaign_id": campaign_id, "item_id": "bandage"}, player),
        buy({"campaign_id": campaign_id, "item_id": "bandage"}, player),
        return_exceptions=True,
    )
    ok = [o for o in outcomes if isinstance(o, dict) and o.get("ok")]
    assert len(ok) == 1
    # The loser is either rejected on stock or told to retry while the lock is held.
    assert len(outcomes) - len(ok) == 1
    async with engine.mutate(campaign_id, player["id"]) as m:
        assert m.state["shop_stock"]["bandage"] == 0
        assert m.member["character"]["currency"]["coins"] == 950


async def test_unknown_text_becomes_narrated_reaction():
    from server.packs import load_pack
    pack = load_pack('refuge')
    player = {"user_id": 1, "character": {"name": "Игрок", "location": "apartment"}}
    order = await engine.parse_intent(pack, "А что, если пойти туда, где свет?", {"world": {"locations": []}}, player)
    assert order["action_id"] == "react"
    assert order["raw_text"].startswith("А что")


async def test_scene_summary_is_written(player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    async with engine.mutate(campaign_id, player["id"]) as m:
        m.state["scene_summary"] = {"text": "Старая сцена", "step_range": [1, 1], "location": "apartment"}
    await engine.submit_order(campaign_id, player["id"], "Осмотреться")
    async with engine.mutate(campaign_id, player["id"]) as m:
        summary = m.state.get("scene_summary")
        assert summary and "text" in summary


async def test_kill_zombie_is_understood_deterministically():
    from server.packs import load_pack
    pack = load_pack('refuge')
    nearby = [{
        'id': 'zombie#1', 'name': 'Заражённый',
        'aliases': ['Заражённый', 'zombie', 'зомби', 'заражённый'],
    }]
    order = engine._deterministic_intent('Убиваю зомби', pack, nearby)
    assert order is not None
    assert order['action_id'] == 'attack'
    assert order['target'] == 'zombie#1'
