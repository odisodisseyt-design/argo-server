"""Checks for the story/narration/client-facing quality fixes."""
from server import engine
from server.packs import load_pack


async def test_turn_does_not_wait_for_narration(client, player, monkeypatch):
    scheduled = []
    monkeypatch.setattr("server.main._schedule_resolution",
                        lambda campaign_id, step_no: scheduled.append((campaign_id, step_no)))
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    response = await client.post(
        "/api/turn", headers=player["headers"],
        json={"campaign_id": campaign_id, "command": "Осмотреться"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["order_saved"] is True
    assert body["step_no"] == 1
    assert "needs_resolution" not in body
    assert scheduled == [(campaign_id, 1)]


async def test_inventory_actions(client, player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    headers = player["headers"]

    equipped = await client.post("/api/inventory/equip", headers=headers,
                                 json={"campaign_id": campaign_id, "item_id": "knife_hunting"})
    assert equipped.status_code == 200
    assert equipped.json()["ok"] is True
    assert equipped.json()["character"]["equipment"]["weapon"] == "knife_hunting"

    unequipped = await client.post("/api/inventory/unequip", headers=headers,
                                   json={"campaign_id": campaign_id, "slot": "weapon"})
    assert unequipped.json()["character"]["equipment"].get("weapon") in (None, "bat")

    refused = await client.post("/api/inventory/equip", headers=headers,
                                json={"campaign_id": campaign_id, "item_id": "water_5l"})
    assert refused.json()["error"] == "NOT_EQUIPPABLE"

    used = await client.post("/api/inventory/use", headers=headers,
                             json={"campaign_id": campaign_id, "item_id": "bandage"})
    assert used.status_code == 200
    assert used.json()["ok"] is True
    bandages = [item for item in used.json()["character"]["inventory"] if item["id"] == "bandage"]
    assert bandages[0]["quantity"] == 2


async def test_shop_buy_updates_character(client, player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    async with engine.mutate(campaign_id, player["id"]) as m:
        m.member["character"].setdefault("currency", {})["coins"] = 1000
    response = await client.post("/api/shop/buy", headers=player["headers"],
                                 json={"campaign_id": campaign_id, "item_id": "bandage"})
    assert response.status_code == 200
    assert response.json().get("ok") is True, response.text
    assert response.json()["stock_left"] >= 0
    ids = [item["id"] for item in response.json()["character"]["inventory"]]
    assert "bandage" in ids


async def test_story_is_grounded_in_the_city(client, player):
    pack = load_pack("refuge")
    manifest = pack["manifest"]
    assert "Арго" in manifest["name"]
    assert "океан" not in manifest["name"].lower()
    assert "океан" not in manifest.get("intro", "").lower()
    assert "океан" not in manifest.get("description", "").lower()

    campaign_id = await engine.create_campaign("refuge", "Тест", player, [], character_name="Илья")
    state = await client.get(f"/api/campaign/{campaign_id}", headers=player["headers"])
    body = state.json()
    assert body["character"]["name"] == "Илья"
    assert "океан" not in body["description"].lower()
    assert "океан" not in body["intro"].lower()
    assert body["living"]["era_name"] == "Пробуждение"
    assert any(faction["name"] == "Странники осколков" for faction in body["living"]["factions"])


async def test_map_exposes_only_russian_labels(client, player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    response = await client.get(f"/api/campaign/{campaign_id}", headers=player["headers"])
    nodes = response.json()["map_nodes"]
    assert nodes
    raw_biomes = {"urban", "road", "forest", "underground", "industrial",
                  "ocean", "techno", "ark", "admin", "refuge"}
    for node in nodes:
        assert node["biome"] not in raw_biomes
        assert node["biome"]
        assert any(word.isalpha() and ord(word[0]) > 127 for word in node["danger"].split())


async def test_client_items_are_explained(client, player):
    campaign_id = await engine.create_campaign("refuge", "Тест", player, [])
    response = await client.get(f"/api/campaign/{campaign_id}", headers=player["headers"])
    items = {item["id"]: item for item in response.json()["character"]["inventory"]}
    assert items["bat"]["description"]
    assert items["bat"]["rarity"] == "обычный"
    assert items["bat"]["equippable"] == "weapon"
    assert items["bat"]["type"] == "Оружие"
    assert "properties" not in items["bat"]
    assert items["bandage"]["usable"] is True
