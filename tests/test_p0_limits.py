import pytest

from server import db, engine, limits
from server.config import settings


async def test_text_length_limit_via_api(client, player):
    campaign = await engine.create_campaign("refuge", "Тест", player, [])
    response = await client.post(
        "/api/turn", headers=player["headers"],
        json={"campaign_id": campaign, "command": "а" * (settings.turn_max_chars + 1)},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "TEXT_TOO_LONG"


async def test_turn_rate_minute_and_hour(player):
    for _ in range(settings.orders_per_minute):
        await limits.check_turn_rate(player["id"])
    with pytest.raises(limits.RateLimited):
        await limits.check_turn_rate(player["id"])


async def test_turn_rate_hour(player):
    now = __import__("time").time()
    await db.execute(
        "INSERT INTO rate_limits(user_id,bucket,window_start,count) VALUES(?,?,?,?)",
        (player["id"], "turn_minute", now - now % 60, 0),
    )
    await db.execute(
        "INSERT INTO rate_limits(user_id,bucket,window_start,count) VALUES(?,?,?,?)",
        (player["id"], "turn_hour", now - now % 3600, settings.orders_per_hour),
    )
    with pytest.raises(limits.RateLimited):
        await limits.check_turn_rate(player["id"])


async def test_campaign_cap_and_delete(client, player, second_player):
    for i in range(settings.max_active_campaigns):
        await engine.create_campaign("refuge", f"Игра {i}", player, [])
    with pytest.raises(limits.CampaignLimit):
        await engine.create_campaign("refuge", "Лишняя", player, [])

    rows = await db.fetchall(
        "SELECT campaign_id AS id FROM campaign_members WHERE user_id=? AND status='active' LIMIT 1",
        (player["id"],),
    )
    campaign_id = rows[0]["id"]
    response = await client.post(
        "/api/campaign/delete", headers=player["headers"],
        json={"campaign_id": campaign_id},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_other_player_keeps_campaign_after_delete(player, second_player):
    campaign = await engine.create_campaign("refuge", "Кооп", player, [second_player])
    await engine.delete_campaign(campaign, player["id"])
    row = await db.fetchone(
        "SELECT status FROM campaign_members WHERE campaign_id=? AND user_id=?",
        (campaign, second_player["id"]),
    )
    assert row["status"] == "active"
