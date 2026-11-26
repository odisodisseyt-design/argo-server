from __future__ import annotations

from typing import Any

ERA_ORDER = ["E0", "E1", "E2", "E3", "E4", "E5"]


def era_index(era: str | None) -> int:
    try:
        return ERA_ORDER.index(str(era or "E0"))
    except ValueError:
        return 0


def _flags(state: dict[str, Any]) -> set[str]:
    raw = state.get("flags", {})
    if isinstance(raw, dict):
        return {str(k) for k, v in raw.items() if v}
    if isinstance(raw, (list, tuple, set)):
        return {str(x) for x in raw}
    return set()


def _knowledge(state: dict[str, Any]) -> set[str]:
    raw = state.get("knowledge", [])
    if isinstance(raw, dict):
        return {str(k) for k, v in raw.items() if v}
    return {str(x) for x in raw or []}


def context(pack: dict[str, Any], state: dict[str, Any], member: dict[str, Any] | None) -> dict[str, Any]:
    character = (member or {}).get("character", {})
    refuge = state.get("refuge", {}) or {}
    time_data = (state.get("world", {}) or {}).get("time", {}) or {}
    living = state.get("living", {}) or {}
    return {
        "era": str(state.get("era", "E0")),
        "flags": _flags(state),
        "knowledge": _knowledge(state),
        "refuge_level": int(refuge.get("level", 0) or 0),
        "refuge_status": str(refuge.get("status", "none")),
        "player_level": int(character.get("level", 1) or 1),
        "class_id": character.get("class_id"),
        "class_category": character.get("class_category"),
        "factions": state.get("factions", {}) or {},
        "hour": int(time_data.get("hour", 12) or 12),
        "day": int(time_data.get("day", 1) or 1),
        "weather": str(living.get("weather", (state.get("world", {}) or {}).get("weather", "clear"))),
        "location": character.get("location"),
    }


def check(requires: dict[str, Any] | None, ctx: dict[str, Any]) -> bool:
    """Evaluate a data-driven requirement block. Missing block means allowed."""
    if not requires:
        return True
    if not isinstance(requires, dict):
        return True

    if "all" in requires and not all(check(item, ctx) for item in requires.get("all", [])):
        return False
    if "any" in requires and not any(check(item, ctx) for item in requires.get("any", [])):
        return False
    if "not" in requires and check(requires.get("not"), ctx):
        return False

    if "era" in requires:
        wanted = requires["era"]
        if isinstance(wanted, (list, tuple)):
            if ctx["era"] not in {str(x) for x in wanted}:
                return False
        elif era_index(ctx["era"]) < era_index(str(wanted)):
            return False

    for flag in requires.get("flags", []) or []:
        if str(flag) not in ctx["flags"]:
            return False
    for flag in requires.get("not_flags", []) or []:
        if str(flag) in ctx["flags"]:
            return False
    for item in requires.get("knowledge", []) or []:
        if str(item) not in ctx["knowledge"]:
            return False
    if "refuge_level" in requires and ctx["refuge_level"] < int(requires["refuge_level"]):
        return False
    if "player_level" in requires and ctx["player_level"] < int(requires["player_level"]):
        return False
    if "refuge_status" in requires and ctx["refuge_status"] != str(requires["refuge_status"]):
        return False

    faction = requires.get("faction")
    if isinstance(faction, dict):
        fid = str(faction.get("id", ""))
        minimum = int(faction.get("min", 0))
        current = int((ctx["factions"].get(fid) or {}).get("rep", 0))
        if current < minimum:
            return False
    elif isinstance(faction, str):
        if faction not in ctx["factions"]:
            return False

    classes = requires.get("class")
    if classes:
        allowed = {str(x) for x in (classes if isinstance(classes, list) else [classes])}
        if ctx.get("class_id") not in allowed and ctx.get("class_category") not in allowed:
            return False

    time_req = requires.get("time")
    if isinstance(time_req, dict):
        low = int(time_req.get("min_hour", 0))
        high = int(time_req.get("max_hour", 23))
        hour = ctx["hour"]
        if low <= high:
            if not (low <= hour <= high):
                return False
        elif not (hour >= low or hour <= high):
            return False

    weather_req = requires.get("weather")
    if weather_req:
        allowed = {str(x) for x in (weather_req if isinstance(weather_req, list) else [weather_req])}
        if ctx["weather"] not in allowed:
            return False
    return True


def filter_entries(entries: list[dict[str, Any]], ctx: dict[str, Any],
                   key: str = "requires") -> list[dict[str, Any]]:
    return [entry for entry in entries if check(entry.get(key), ctx)]
