"""Preview and Resolve must share exactly the same requirement rules."""
import copy

from server import engine, resolver
from server.packs import load_pack


def _fixture(with_environment: bool = True):
    pack = copy.deepcopy(load_pack("refuge"))
    pack["actions"]["look"]["environment_modifiers"] = {"indoors": 1}
    character = copy.deepcopy(pack["initial_state"]["character_template"])
    engine._normalize_character(pack, character)
    member = {"user_id": 1, "character": character}
    state = {
        "world": {
            "environment": {"indoors": with_environment},
            "locations": [],
        },
        "era": "E0",
    }
    order = {"action_id": "look", "target": None, "method_tags": []}
    return pack, state, member, order


def test_requirement_helper_is_pure_and_repeatable():
    pack, state, member, order = _fixture()
    a = resolver._requirement(pack, state, member["character"], "look", order, None)
    b = resolver._requirement(pack, state, member["character"], "look", order, None)
    assert a == b


def test_preview_accounts_for_environment_modifiers():
    pack, state, member, order = _fixture(with_environment=True)
    plain = copy.deepcopy(state)
    plain["world"]["environment"]["indoors"] = False

    with_env = resolver.preview(pack, state, member, order)
    without_env = resolver.preview(pack, plain, member, order)
    assert with_env["margin"] == without_env["margin"] - 1
    assert "indoors" in [item["label"] for item in with_env["modifiers"]]
    assert "indoors" not in [item["label"] for item in without_env["modifiers"]]


def test_preview_and_resolve_share_requirement():
    pack, state, member, order = _fixture()
    preview = resolver.preview(pack, state, member, order)
    resolved = resolver.resolve(pack, state, member, order, "p0:preview:1")
    assert resolved["status"] == "resolved"
    assert preview["margin"] == resolved["margin"]
    assert [item["label"] for item in preview["modifiers"]] == [
        item["label"] for item in resolved["modifiers"]
    ]


def test_preview_and_resolve_boundaries(monkeypatch):
    pack, state, member, order = _fixture()
    monkeypatch.setattr(resolver, "_capability", lambda *args: (10.0, []))
    sure = resolver.preview(pack, state, member, order)
    resolved = resolver.resolve(pack, state, member, order, "p0:sure")
    assert sure["chance"] == 100
    assert resolved["result"] == "success"
    assert resolved["roll"] is None

    monkeypatch.setattr(resolver, "_capability", lambda *args: (-5.0, []))
    unlikely = resolver.preview(pack, state, member, order)
    resolved = resolver.resolve(pack, state, member, order, "p0:unlikely")
    assert unlikely["chance"] == 0
    assert resolved["result"] == "failure"
    assert resolved["roll"] is None

    monkeypatch.setattr(resolver, "_capability", lambda *args: (1.0, []))
    band = resolver.preview(pack, state, member, order)
    first = resolver.resolve(pack, state, member, order, "p0:band")
    second = resolver.resolve(pack, state, member, order, "p0:band")
    assert band["chance"] == int(resolver.BAND_PROBABILITY[0] * 100)
    assert first["roll"] is not None
    assert first["result"] == second["result"]
    assert first["roll"] == second["roll"]
