from server import db, engine, models
from server.config import settings


async def test_catalog_hides_internals(client, player):
    response = await client.get("/api/models", headers=player["headers"])
    assert response.status_code == 200
    models_view = response.json()["models"]
    assert models_view
    first = models_view[0]
    assert first["id"] == settings.default_model_id
    assert "provider" not in first
    assert "intent_model" not in first
    assert "api_key" not in first


async def test_default_model_is_fastest(client, player):
    campaign = await engine.create_campaign("refuge", "Тест", player, [])
    response = await client.get(f"/api/campaign/{campaign}", headers=player["headers"])
    assert response.json()["model"]["id"] == settings.default_model_id == "fast"


async def test_only_founder_can_change_model(client, player, second_player):
    campaign = await engine.create_campaign("refuge", "Тест", player, [second_player])
    denied = await client.post(
        f"/api/campaign/{campaign}/model", headers=second_player["headers"],
        json={"model_id": "balanced"},
    )
    assert denied.status_code == 403
    ok = await client.post(
        f"/api/campaign/{campaign}/model", headers=player["headers"],
        json={"model_id": "balanced"},
    )
    assert ok.status_code == 200
    row = await db.fetchone("SELECT model_id FROM campaigns WHERE id=?", (campaign,))
    assert row["model_id"] == "balanced"


async def test_unavailable_model_rejected(client, player):
    response = await client.post(
        "/api/campaign/new", headers=player["headers"],
        json={"pack_id": "refuge", "name": "Тест", "model_id": "does-not-exist"},
    )
    assert response.status_code == 422
