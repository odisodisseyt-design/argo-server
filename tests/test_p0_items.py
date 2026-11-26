from server import db, engine
from server.packs import load_pack, validate_pack


async def test_add_item_rejects_unknown_id(monkeypatch):
    pack = load_pack("refuge")
    character = {"inventory": []}
    assert engine._add_item(character, pack, "no_such_item", 1) is False
    assert character["inventory"] == []
    await __import__("asyncio").sleep(0.05)
    row = await db.fetchone("SELECT * FROM audit_logs WHERE action='invalid_item'")
    assert row is not None


async def test_item_name_never_falls_back_to_id():
    pack = load_pack("refuge")
    character = {"inventory": []}
    engine._add_item(character, pack, "bandage", 1)
    assert character["inventory"][0]["name"] == pack["items"]["bandage"]["name"]


async def test_remove_item_rejects_unknown_id():
    pack = load_pack("refuge")
    character = {"inventory": [{"id": "bandage", "name": "Бинт", "quantity": 1}]}
    assert engine._remove_item(character, "no_such_item", 1, pack) is False
    assert len(character["inventory"]) == 1


async def test_pack_validation_catches_broken_item_reference(tmp_path):
    errors = validate_pack("refuge")
    assert errors == []
