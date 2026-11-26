from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import PACKS_DIR

REQUIRED_JSON = [
    "manifest.json", "world.json", "rules.json", "conditions.json",
    "actions.json", "npcs.json", "shop.json", "style.json",
    "ui_manifest.json", "initial_state.json",
]
REQUIRED_TEXT = ["lore.md"]
OPTIONAL_JSON = [
    "narration_templates.json", "quests.json", "eras.json", "unlocks.json",
    "classes.json", "skills.json", "items.json", "mobs.json",
    "loot_tables.json", "recipes.json", "worlds.json", "events.json",
    "factions.json",
]

_cache: dict[str, dict[str, Any]] = {}


def list_packs() -> list[str]:
    return sorted(p.name for p in PACKS_DIR.iterdir() if p.is_dir() and (p / "manifest.json").exists())


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_pack(pack_id: str) -> list[str]:
    root = PACKS_DIR / pack_id
    errors: list[str] = []
    if not root.is_dir():
        return [f"pack '{pack_id}' not found"]
    for name in REQUIRED_JSON + REQUIRED_TEXT:
        if not (root / name).exists():
            errors.append(f"missing {name}")
    if errors:
        return errors
    try:
        actions = _read_json(root / "actions.json")
        conditions = _read_json(root / "conditions.json")
        rules = _read_json(root / "rules.json")
        ui = _read_json(root / "ui_manifest.json")
        item_ids: set[str] = set()
        items_path = root / "items.json"
        if items_path.exists():
            items = _read_json(items_path)
            item_ids = set(items if isinstance(items, list) else items.keys())
    except Exception as exc:  # noqa: BLE001
        return [f"invalid json: {exc}"]
    if not isinstance(actions, dict) or not actions:
        errors.append("actions.json must be a non-empty object")
    if not isinstance(conditions, dict):
        errors.append("conditions.json must be an object")
    if "critical_band" not in rules:
        errors.append("rules.json missing critical_band")
    if "features" not in ui:
        errors.append("ui_manifest.json missing features")
    for aid, action in actions.items():
        for key in ("verbs", "attribute", "base_requirement", "outcomes", "refusal_phrases"):
            if key not in action:
                errors.append(f"action '{aid}' missing {key}")
    for cid, cond in conditions.items():
        if "blocks" not in cond or "modifiers" not in cond:
            errors.append(f"condition '{cid}' missing blocks/modifiers")

    if item_ids:
        classes_path = root / "classes.json"
        if classes_path.exists():
            classes = _read_json(classes_path)
            entries = classes.get("classes", []) if isinstance(classes, dict) else classes
            for entry in entries:
                for gear in entry.get("gear", []) or []:
                    ref = str(gear.get("item") if isinstance(gear, dict) else gear)
                    if ref and ref not in item_ids:
                        errors.append(f"class '{entry.get('id')}' references unknown item '{ref}'")
                for item_id in (entry.get("equipment") or {}).values():
                    if str(item_id) not in item_ids:
                        errors.append(f"class '{entry.get('id')}' references unknown item '{item_id}'")
        loot_path = root / "loot_tables.json"
        if loot_path.exists():
            tables = _read_json(loot_path)
            for table_id, table in tables.items():
                for entry in table.get("entries", []) or []:
                    ref = entry.get("item")
                    if ref and str(ref) not in item_ids:
                        errors.append(f"loot table '{table_id}' references unknown item '{ref}'")
    return errors


def load_pack(pack_id: str) -> dict[str, Any]:
    if pack_id in _cache:
        return _cache[pack_id]
    errors = validate_pack(pack_id)
    if errors:
        raise ValueError("; ".join(errors))
    root = PACKS_DIR / pack_id
    pack: dict[str, Any] = {"id": pack_id}
    for name in REQUIRED_JSON:
        pack[name.removesuffix(".json")] = _read_json(root / name)
    for name in OPTIONAL_JSON:
        if (root / name).exists():
            pack[name.removesuffix(".json")] = _read_json(root / name)
    pack["lore"] = (root / "lore.md").read_text(encoding="utf-8")
    _cache[pack_id] = pack
    return pack


def invalidate(pack_id: str | None = None) -> None:
    if pack_id:
        _cache.pop(pack_id, None)
    else:
        _cache.clear()
