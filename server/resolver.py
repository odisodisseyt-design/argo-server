from __future__ import annotations

import hashlib
import random
from typing import Any

from . import catalog, gates, worldsim

BAND_PROBABILITY = {1: 0.85, 0: 0.50, -1: 0.15}


def seeded_roll(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big")).randint(1, 100)


def _condition_map(pack: dict[str, Any], character: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in character.get("conditions", []):
        cid = entry if isinstance(entry, str) else (entry or {}).get("id")
        cond = pack["conditions"].get(cid) if cid else None
        if cond:
            result[cid] = cond
    return result


def _requirement(pack: dict[str, Any], state: dict[str, Any],
                 character: dict[str, Any], action_id: str, order: dict[str, Any],
                 target_item: dict[str, Any] | None) -> tuple[float, list[dict[str, Any]]]:
    """Single source of truth for requirement and modifiers.

    Both resolve() and preview() must use exactly the same rules. Extracting
    this changes no balance: the formula and modifier order are unchanged.
    """
    action = pack["actions"][action_id]
    requirement = float(action["base_requirement"])
    modifiers: list[dict[str, Any]] = []
    for cid, cond in _condition_map(pack, character).items():
        delta = cond.get("modifiers", {}).get(action_id, 0)
        if delta:
            requirement += delta
            modifiers.append({"label": cond.get("name", cid), "value": delta})
    for tag in order.get("method_tags", []):
        delta = action.get("method_modifiers", {}).get(tag, 0)
        if delta:
            requirement += delta
            modifiers.append({"label": tag, "value": delta})
    env = state.get("world", {}).get("environment", {})
    for key, delta in action.get("environment_modifiers", {}).items():
        if env.get(key):
            requirement += delta
            modifiers.append({"label": key, "value": delta})
    if action_id == "attack" and target_item and target_item.get("kind") in ("mob", "npc"):
        level = int(target_item.get("level", 1) or 1)
        requirement += max(0, level - 1) * 1.5
        modifiers.append({"label": "Уровень цели", "value": max(0, level - 1) * 1.5})
    return requirement, modifiers


def _refusal(reason: str, phrase: str) -> dict[str, Any]:
    return {
        "status": "refused",
        "reason_code": reason,
        "text": phrase,
        "effects": {},
    }


def _nearby(pack: dict[str, Any], state: dict[str, Any], member: dict[str, Any]) -> list[dict[str, Any]]:
    return worldsim.nearby_for(pack, state, member)


def _resolve_target(pack: dict[str, Any], state: dict[str, Any], member: dict[str, Any],
                    target: str | None) -> dict[str, Any] | None:
    if not target:
        return None
    for item in _nearby(pack, state, member):
        if item.get("id") == target or target in item.get("aliases", []):
            return item
    return None


def _equipment_bonus(pack: dict[str, Any], character: dict[str, Any], action_id: str) -> tuple[float, list[dict[str, Any]]]:
    bonus = 0.0
    modifiers: list[dict[str, Any]] = []
    equipment = character.get("equipment", {}) or {}
    for slot, item_id in equipment.items():
        data = catalog.item(pack, item_id)
        if not data:
            continue
        if action_id == "attack" and slot == "weapon":
            value = float(data.get("attack_bonus", 0) or 0)
            if value:
                bonus += value
                modifiers.append({"label": f"Оружие: {data.get('name', item_id)}", "value": value})
        if slot == "armor":
            value = float(data.get("armor", 0) or 0)
            if value and action_id in ("dodge", "attack", "move"):
                bonus += value * 0.5
                modifiers.append({"label": f"Защита: {data.get('name', item_id)}", "value": value * 0.5})
    for perk in character.get("perks", []) or []:
        effect = perk.get("effect", {}) if isinstance(perk, dict) else {}
        if action_id == "attack" and effect.get("attack_bonus"):
            value = float(effect["attack_bonus"])
            bonus += value
            modifiers.append({"label": perk.get("name", "Особенность"), "value": value})
        if action_id in ("dodge", "move") and effect.get("armor"):
            value = float(effect["armor"]) * 0.5
            bonus += value
            modifiers.append({"label": perk.get("name", "Особенность"), "value": value})
    return bonus, modifiers


def _capability(pack: dict[str, Any], character: dict[str, Any], action: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    action_id = action.get("id", "")
    attribute = action.get("attribute", "end")
    capability = float(character.get("attributes", {}).get(attribute, 0))
    modifiers: list[dict[str, Any]] = []
    for skill in character.get("skills", []):
        if skill.get("attribute") == attribute:
            value = float(skill.get("bonus", 0))
            if value:
                capability += value
    eq_bonus, eq_mods = _equipment_bonus(pack, character, action_id)
    capability += eq_bonus
    modifiers.extend(eq_mods)
    return capability, modifiers


def resolve(
    pack: dict[str, Any],
    state: dict[str, Any],
    member: dict[str, Any],
    order: dict[str, Any],
    seed: str,
) -> dict[str, Any]:
    """Deterministic action resolution. No LLM decisions happen here."""
    character = member["character"]
    action_id = order.get("action_id", "")
    action = pack["actions"].get(action_id)
    phrases = action.get("refusal_phrases", {}) if action else {}
    if not action:
        return _refusal("UNKNOWN_ACTION", "Система не распознала такое действие. Уточни, что ты хочешь сделать.")

    if not character.get("alive", True):
        return _refusal("DEAD", "Мёртвый не может действовать.")
    if character.get("pending_class_selection"):
        return _refusal("CLASS_SELECTION_REQUIRED", "Сначала выбери класс. Система не принимает приказы без пути.")

    ctx = gates.context(pack, state, member)
    if not gates.check(action.get("requires"), ctx):
        return _refusal("NOT_READY", phrases.get("NOT_READY", "Для этого ещё не время."))

    conditions = _condition_map(pack, character)
    blocked = [c for c in conditions.values()
               if "*" in c.get("blocks", []) or action_id in c.get("blocks", [])]
    if blocked:
        phrase = next(
            (c.get("messages", {}).get(action_id) or c.get("messages", {}).get("*")
             for c in blocked if (c.get("messages", {}).get(action_id) or c.get("messages", {}).get("*"))),
            phrases.get("BLOCKED_BY_CONDITION", "Так сейчас сделать невозможно."),
        )
        return _refusal("BLOCKED_BY_CONDITION", phrase)

    target = order.get("target")
    target_item: dict[str, Any] | None = None
    if action.get("needs_target"):
        if not target:
            return _refusal("UNKNOWN_TARGET", phrases.get("UNKNOWN_TARGET", "Укажи, к чему именно."))
        target_item = _resolve_target(pack, state, member, target)
        if not target_item:
            inventory = {item.get("id") for item in character.get("inventory", [])}
            if target in inventory and action_id in ("use", "read", "take"):
                target_item = {"id": target, "kind": "inventory", "name": target}
            else:
                return _refusal("UNKNOWN_TARGET", phrases.get("UNKNOWN_TARGET", "Этого здесь нет."))
        target = target_item.get("id")

    if action.get("target_kinds") and target_item and target_item.get("kind") not in action["target_kinds"]:
        return _refusal("WRONG_TARGET", phrases.get("WRONG_TARGET", "Так с этим не получится."))

    if action.get("max_reach_m") and target_item:
        dist = float(target_item.get("distance_m", 0) or 0)
        if dist > float(action["max_reach_m"]):
            return _refusal("OUT_OF_REACH", phrases.get("OUT_OF_REACH", "Слишком далеко."))

    cost = action.get("cost", {}) or {}
    if cost.get("currency") and character.get("currency", {}).get(cost["currency"], 0) < cost["amount"]:
        return _refusal("INSUFFICIENT_RESOURCE", phrases.get("INSUFFICIENT_RESOURCE", "Не хватает средств."))
    if cost.get("mana") and character.get("resources", {}).get("mana", 0) < cost["mana"]:
        return _refusal("INSUFFICIENT_RESOURCE", phrases.get("INSUFFICIENT_RESOURCE", "Мана на исходе."))

    requirement, modifiers = _requirement(
        pack, state, character, action_id, order, target_item
    )
    capability, eq_mods = _capability(pack, character, action)
    modifiers.extend(eq_mods)
    margin = capability - requirement

    band = pack["rules"].get("critical_band", {"sure": 2, "unlikely": -1})
    sure = int(band.get("sure", 2))
    unlikely = int(band.get("unlikely", -1))

    if margin >= sure:
        result, roll = "success", None
    elif margin <= unlikely - 1:
        result, roll = "failure", None
    else:
        probability = BAND_PROBABILITY.get(int(margin), 0.5)
        roll = seeded_roll(seed)
        result = "success" if roll <= int(probability * 100) else "failure"

    outcome = action["outcomes"].get(result, action["outcomes"]["failure"])
    text = outcome.get("text", "Действие совершено.")
    for key, value in {
        "actor": character.get("name", "Игрок"),
        "target": (target_item or {}).get("name", target or ""),
        "npc": (target_item or {}).get("name", target or ""),
    }.items():
        text = text.replace("{" + key + "}", str(value))

    effects = dict(outcome.get("effects", {}) or {})
    if action_id == "move" and target_item and target_item.get("exit_to"):
        effects["move_to"] = target_item["exit_to"]
    if action_id == "attack" and target_item and target_item.get("kind") == "mob":
        effects["defeat_target"] = True
        effects["mob_id"] = target_item.get("mob")
        effects["loot_table"] = (catalog.mob(pack, target_item.get("mob")) or {}).get("loot_table", "zombie_common")
    if action_id == "attack" and target_item and target_item.get("kind") == "npc":
        effects["defeat_npc"] = target_item.get("npc_id")

    return {
        "status": "resolved",
        "action_id": action_id,
        "target": target,
        "target_name": (target_item or {}).get("name", ""),
        "target_kind": (target_item or {}).get("kind", ""),
        "result": result,
        "reason_code": "OK",
        "text": text,
        "roll": roll,
        "margin": margin,
        "requirement": requirement,
        "capability": capability,
        "modifiers": modifiers,
        "effects": effects,
        "xp": outcome.get("xp", 0),
    }


def preview(
    pack: dict[str, Any], state: dict[str, Any], member: dict[str, Any], order: dict[str, Any]
) -> dict[str, Any]:
    """Same maths as resolve, but does not apply anything and does not roll."""
    character = member["character"]
    action = pack["actions"].get(order.get("action_id", ""))
    if not action:
        return {"chance": 0, "modifiers": [], "note": "unknown action"}
    target_item = _resolve_target(pack, state, member, order.get("target"))
    requirement, modifiers = _requirement(
        pack, state, character, order["action_id"], order, target_item
    )
    capability, eq_mods = _capability(pack, character, action)
    modifiers.extend(eq_mods)
    margin = capability - requirement
    band = pack["rules"].get("critical_band", {"sure": 2, "unlikely": -1})
    if margin >= band.get("sure", 2):
        chance = 100
    elif margin <= band.get("unlikely", -1) - 1:
        chance = 0
    else:
        chance = int(BAND_PROBABILITY.get(int(margin), 0.5) * 100)
    return {"action_id": order["action_id"], "chance": chance, "modifiers": modifiers, "margin": margin}
