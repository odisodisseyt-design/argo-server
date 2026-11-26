"""The world pack must separate book canon from campaign decisions."""
import json
from pathlib import Path

from server.packs import load_pack

PACK = Path(__file__).resolve().parents[1] / "server" / "packs" / "refuge"


def _provenance() -> dict:
    return json.loads((PACK / "provenance.json").read_text(encoding="utf-8"))


def test_provenance_structure():
    doc = _provenance()
    assert doc["version"] == "1.0"
    assert set(doc["status_values"]) >= {"direct", "direct_example", "unknown", "campaign_decision"}
    assert set(doc["world_scope_values"]) >= {"earth", "other_world", "spatial_fragment", "unknown"}
    assert doc["canon_facts"] and doc["campaign_decisions"]
    for fact in doc["canon_facts"]:
        assert fact["source_status"] in doc["status_values"]
        assert fact["evidence"]
        assert "book" in fact and "chapter" in fact
    for decision in doc["campaign_decisions"]:
        assert decision["source_status"] == "campaign_decision"
        assert decision["reason"]


def test_late_worlds_are_canon_but_not_available_at_start():
    doc = _provenance()
    entities = doc["entities"]
    for era in ("E2", "E3", "E4", "E5"):
        record = entities["eras"][era]
        assert record["source_status"] == "direct"
        assert record["world_scope"] in ("other_world", "spatial_fragment", "unknown")
        assert record["available_at_campaign_start"] is False
    for group in ("locations", "items", "mobs"):
        for record in entities[group].values():
            if record["world_scope"] != "earth":
                # Late worlds are canon (direct or inferred), never our
                # invention, and never available at the start.
                assert record["available_at_campaign_start"] is False
                assert record["source_status"] in ("direct", "inference")
            elif record["available_at_campaign_start"] is False:
                # An Earth place may open later in the campaign, but then it is
                # our decision rather than canon lore.
                assert record["source_status"] == "campaign_decision"

    pack = load_pack("refuge")
    names = {item["name"] for item in pack["eras"]["eras"].values()}
    assert {"Пространственный океан", "Ковчеги", "Техномир", "Администратор"} <= names


def test_early_story_is_marked_as_campaign_decision():
    doc = _provenance()
    assert doc["entities"]["eras"]["E0"]["source_status"] == "campaign_decision"
    assert doc["entities"]["factions"]["survivors"]["source_status"] == "campaign_decision"
    ids = {item["id"] for item in doc["campaign_decisions"]}
    assert {"cd_start", "cd_classes", "cd_refuge", "cd_body"} <= ids
    manifest = doc_source_manifest()
    assert manifest["provenance"] == "provenance.json"


def doc_source_manifest() -> dict:
    return json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))


def test_pack_names_match_restored_canon():
    pack = load_pack("refuge")
    locations = {item["name"] for item in pack["worlds"]["locations"].values()}
    assert {"Врата в океан", "Осколок мира", "Вход в Ковчег", "Зал Ковчега",
            "Техномир", "Ядро Администратора"} <= locations
    quests = {item["name"] for item in pack["quests"]["quests"]}
    assert {"Ковчеги", "Техномир", "Администратор"} <= quests
