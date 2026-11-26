"""Real Moscow map: projection, hidden places, basemap and player DTO."""
from __future__ import annotations

import copy

from server import engine, geo, worldsim
from server.packs import load_pack


def test_projection_matches_basemap_frame():
    x0, y0 = geo.project(55.95, 37.30)
    x1, y1 = geo.project(55.55, 37.95)
    assert (x0, y0) == (0.0, 0.0)
    assert x1 == geo.MAP_WIDTH
    assert abs(y1 - geo.MAP_HEIGHT) < 0.2

    x, y = geo.project(55.632, 37.608)
    assert 0 < x < geo.MAP_WIDTH
    assert 0 < y < geo.MAP_HEIGHT


def test_location_view_hides_internal_data():
    location = {
        "id": "street", "name": "Варшавское шоссе", "district": "Чертаново Северное",
        "lat": 55.633, "lon": 37.611, "biome": "urban", "danger": 2,
        "population": {"zombie": 3}, "loot_table": "street", "objects": [{"id": "x"}],
        "exits": [{"to": "pharmacy"}], "requires": {"flag": "secret"},
    }
    view = geo.location_view(location)
    assert set(view) == {
        "id", "name", "district", "lat", "lon", "x", "y", "biome", "danger",
    }
    assert view["biome"] == "Город"
    assert view["danger"] == "Осторожно"
    assert view["x"] == geo.project(55.633, 37.611)[0]


def test_basemap_is_real_moscow():
    data = geo.load_basemap("refuge")
    assert data is not None
    assert data["width"] == geo.MAP_WIDTH
    assert data["height"] == geo.MAP_HEIGHT
    assert data["attribution"] == "© OpenStreetMap contributors"
    assert data["roads"]["motorway"]
    assert data["roads"]["primary"]
    assert data["water"]
    assert data["rail"]
    assert any(label["text"] == "Кремль" for label in data["labels"])


def test_pack_update_keeps_runtime_exits_and_objects():
    pack = load_pack("refuge")
    state = copy.deepcopy(pack["initial_state"])
    state["members"] = []
    state["world"]["locations"] = worldsim.initial_locations(pack, state)
    street = next(loc for loc in state["world"]["locations"] if loc["id"] == "street")

    street["exits"] = [edge for edge in street["exits"]
                       if edge.get("to") != "paveletsky"]
    street["exits"].append({"to": "runtime_room", "via": "служебный ход"})
    street["objects"] = [{"id": "runtime_prop", "taken": True}]

    worldsim.refresh_locations(pack, state)

    targets = {edge.get("to") for edge in street["exits"]}
    assert "paveletsky" in targets          # new road from the pack
    assert "runtime_room" in targets        # runtime road kept
    assert street["objects"][0] == {"id": "runtime_prop", "taken": True}


async def test_campaign_map_hides_unvisited_places(client, player):
    campaign_id = await engine.create_campaign("refuge", "Карта", player, [])
    headers = player["headers"]

    first = (await client.get(f"/api/campaign/{campaign_id}", headers=headers)).json()
    nodes = {node["id"]: node for node in first["map_nodes"]}
    assert nodes["apartment"]["current"] is True
    assert nodes["apartment"]["visited"] is True
    assert "stairwell" in nodes  # direct road from the start
    assert "kremlin" not in nodes
    assert "paveletsky" not in nodes
    assert all("population" not in node and "loot_table" not in node
               for node in first["map_nodes"])
    assert "population" not in first["anchor"]["location"]
    assert "objects" not in first["anchor"]["location"]
    assert first["anchor"]["location"]["district"] == "Чертаново Северное"

    async with engine.mutate(campaign_id, player["id"]) as m:
        m.member["character"]["location"] = "street"
        for location in m.state["world"]["locations"]:
            if location["id"] in ("apartment", "stairwell", "courtyard", "street"):
                location["visited"] = True

    second = (await client.get(f"/api/campaign/{campaign_id}", headers=headers)).json()
    nodes = {node["id"]: node for node in second["map_nodes"]}
    assert nodes["street"]["current"] is True
    for neighbour in ("pharmacy", "school", "hospital", "road", "paveletsky"):
        assert neighbour in nodes
        assert nodes[neighbour]["visited"] is False
    assert "kremlin" not in nodes
    assert set(nodes["street"]["connections"]) == {
        "courtyard", "pharmacy", "school", "hospital", "road", "paveletsky",
    }
    assert nodes["paveletsky"]["district"] == "Замоскворечье"


async def test_basemap_endpoint_requires_auth(client, player):
    denied = await client.get("/api/map/refuge")
    assert denied.status_code == 401

    response = await client.get("/api/map/refuge", headers=player["headers"])
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "moscow"
    assert data["roads"]["trunk"]
    assert load_pack("refuge")["manifest"]["version"] == "0.5.0"
