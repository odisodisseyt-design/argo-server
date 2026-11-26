from __future__ import annotations

from typing import Any

from . import geo

# Only these keys may ever be sent to a player. Everything else stays on the server.
CHARACTER_KEYS = (
    "id", "name", "level", "xp", "xp_to_next", "unspent_points", "attributes",
    "derived", "resources", "body", "conditions", "inventory", "equipment", "currency",
    "skills", "statuses", "pending_level_up", "alive", "location", "class_id",
    "class_name", "class_category", "perks", "pending_class_selection", "skill_points",
)
ANCHOR_KEYS = ("location", "time", "weather", "visibility_m", "nearby", "environment")


def character_view(member: dict[str, Any]) -> dict[str, Any]:
    character = member["character"]
    view = {key: character[key] for key in CHARACTER_KEYS if key in character}
    view["id"] = member["user_id"]
    view["name"] = character.get("name", f"Игрок {member['user_id']}")
    return view


def anchor_view(state: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    world = state.get("world", {})
    view = {key: world[key] for key in ANCHOR_KEYS
            if key in world and key != "location"}
    loc_id = member["character"].get("location")
    locations = {item.get("id"): item for item in world.get("locations", [])}
    if loc_id and loc_id in locations:
        view["location"] = geo.location_view(locations[loc_id])
    elif isinstance(world.get("location"), dict):
        view["location"] = geo.location_view(world["location"])
    view["player"] = {
        "location": locations.get(loc_id, {}).get("name", world.get("location", {}).get("name", "")),
        "alive": member["character"].get("alive", True),
    }
    others = []
    for other in state.get("members", []):
        if other["user_id"] != member["user_id"]:
            other_loc = other["character"].get("location")
            others.append({
                "name": other["character"].get("name"),
                "location": locations.get(other_loc, {}).get("name", other_loc or ""),
                "alive": other["character"].get("alive", True),
            })
    view["party"] = others
    return view


def presentation_view(state: dict[str, Any]) -> dict[str, Any]:
    data = dict(state.get("presentation", {}))
    living = state.get("living", {})
    if living:
        data["weather"] = living.get("weather", data.get("weather", ""))
        data["time_of_day"] = living.get("time_of_day", data.get("time_of_day", ""))
        data["era"] = state.get("era", "E0")
    data["schema_version"] = data.get("schema_version", 1)
    return data


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


def preview_view(preview: dict[str, Any]) -> dict[str, Any]:
    """Only the player-facing part of a preview.

    margin/modifiers/capability are engine internals and must never leave the
    server, even though the resolver still needs them while planning a turn.
    """
    return {
        "action_id": str(preview.get("action_id", "")),
        "chance": int(preview.get("chance", 0)),
    }


def npc_view(npc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(npc.get("id", "")),
        "name": str(npc.get("name", "")),
        "role": str(npc.get("role", "")),
        "location": npc.get("location"),
        "activity": str(npc.get("activity", "")),
        "disposition": str(npc.get("disposition", "")),
        "alive": bool(npc.get("alive", True)),
    }


def faction_view(faction: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(faction.get("id", "")),
        "name": str(faction.get("name", "")),
        "disposition": str(faction.get("disposition", "")),
        "goal": str(faction.get("goal", "")),
    }


def refuge_view(refuge: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(refuge.get("status", "none")),
        "level": int(refuge.get("level", 0) or 0),
        "name": str(refuge.get("name", "")),
    }


def knowledge_view(items: list[Any]) -> list[str]:
    return [str(item) for item in items if isinstance(item, str)]


def journal_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Journal entries keep only fields the player needs to read and accept them."""
    return [
        {
            "id": row["id"],
            "text": row["text"],
            "status": row["status"],
            "step_no": int(row.get("step_no") or 0),
        }
        for row in rows if row.get("status") in ("canon", "pending", "rejected")
    ]
