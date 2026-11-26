from __future__ import annotations

from typing import Any

from . import gates


def items(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return pack.get("items", {}) or {}


def item(pack: dict[str, Any], item_id: str | None) -> dict[str, Any] | None:
    if not item_id:
        return None
    return items(pack).get(str(item_id))


_KIND_NAMES = {
    "weapon": "Оружие", "melee": "Оружие", "ranged": "Оружие",
    "blade": "Оружие", "blunt": "Оружие", "armor": "Броня",
    "clothing": "Одежда", "gear": "Снаряжение", "consumable": "Расходное",
    "medicine": "Лекарство", "tool": "Инструмент", "material": "Материал",
    "key": "Ключ", "container": "Тайник", "structure": "Для убежища",
    "book": "Книга", "document": "Документ", "misc": "Разное",
}


def item_view(pack: dict[str, Any], item_id: str, qty: int = 1) -> dict[str, Any]:
    data = item(pack, item_id) or {"id": item_id, "name": item_id, "weight_kg": 0}
    kind = _KIND_NAMES.get(str(data.get("kind", "")).lower(), "Предмет")
    return {
        "id": data.get("id", item_id),
        "name": data.get("name", item_id),
        "description": data.get("desc", ""),
        "type": kind,
        "slot": kind,
        "quantity": int(qty),
        "weight": float(data.get("weight_kg", 0) or 0) * int(qty),
        "rarity": "обычный",
    }


def classes(pack: dict[str, Any]) -> list[dict[str, Any]]:
    return (pack.get("classes", {}) or {}).get("classes", []) or []


def class_by_id(pack: dict[str, Any], class_id: str | None) -> dict[str, Any] | None:
    if not class_id:
        return None
    return next((c for c in classes(pack) if c.get("id") == class_id), None)


def available_classes(pack: dict[str, Any], state: dict[str, Any],
                      member: dict[str, Any]) -> list[dict[str, Any]]:
    ctx = gates.context(pack, state, member)
    result = []
    for entry in classes(pack):
        if gates.check(entry.get("requires"), ctx):
            result.append(entry)
    return result


def class_view(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id", ""),
        "name": entry.get("name", ""),
        "category": entry.get("category", ""),
        "category_name": entry.get("category_name", ""),
        "color": entry.get("color", "#C9A227"),
        "era": entry.get("era", "E0"),
        "era_name": entry.get("era_name", ""),
        "available_at_start": bool(entry.get("available_at_start")),
        "role": entry.get("role", ""),
        "description": entry.get("desc", ""),
        "attributes": entry.get("attributes", {}),
        "skills": entry.get("skills", []),
        "gear": entry.get("gear", []),
        "equipment": entry.get("equipment", {}),
        "perks": entry.get("perks", []),
        "evolutions": entry.get("evolutions", []),
        "starting_coins": int(entry.get("starting_coins", 0) or 0),
    }


def skills(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return (pack.get("skills", {}) or {}).get("skills", {}) or {}


def mobs(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return pack.get("mobs", {}) or {}


def mob(pack: dict[str, Any], mob_id: str | None) -> dict[str, Any] | None:
    if not mob_id:
        return None
    return mobs(pack).get(str(mob_id))


def loot_tables(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return pack.get("loot_tables", {}) or {}


def recipes(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return pack.get("recipes", {}) or {}


def worlds(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return (pack.get("worlds", {}) or {}).get("locations", {}) or {}


def events(pack: dict[str, Any]) -> list[dict[str, Any]]:
    return (pack.get("events", {}) or {}).get("events", []) or []


def factions(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return pack.get("factions", {}) or {}


def eras(pack: dict[str, Any]) -> dict[str, Any]:
    return pack.get("eras", {}) or {}


def unlocks(pack: dict[str, Any]) -> dict[str, Any]:
    return pack.get("unlocks", {}) or {}
