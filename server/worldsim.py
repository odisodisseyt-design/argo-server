from __future__ import annotations

import copy
import random
from typing import Any

from . import catalog, gates

DIRECTIONS = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
NPC_NAMES = [
    "Вера", "Гриша", "Толя", "Настя", "Марк", "Лида", "Семён", "Оксана",
    "Илья", "Рита", "Паша", "Зоя", "Костя", "Аня", "Лёша", "Марина",
]
NPC_ROLES = [
    ("выживший", "ищет воду и сухое место"),
    ("беженец", "держит при себе узел с вещами"),
    ("разведчик", "запоминает улицы и лица"),
    ("торговец", "носит с собой мелочь для обмена"),
    ("санитар", "ищет бинты и чистую воду"),
]


def rng_for(campaign_id: str, step_no: int, salt: str = "") -> random.Random:
    return random.Random(f"{campaign_id}:{step_no}:{salt}")


def ensure_living(pack: dict[str, Any], state: dict[str, Any]) -> None:
    living = state.setdefault("living", {})
    living.setdefault("season", "осень")
    living.setdefault("weather", state.get("world", {}).get("weather", "overcast"))
    living.setdefault("temperature_c", 12)
    living.setdefault("events", [])
    living.setdefault("event_history", [])
    living.setdefault("rumors", [])
    living.setdefault("weather_until", 0)
    living.setdefault("time_of_day", "day")
    state.setdefault("era", "E0")
    state.setdefault("flags", {})
    state.setdefault("knowledge", [])
    state.setdefault("npcs", {})
    state.setdefault("ecology", {})
    state.setdefault("factions", {})
    state.setdefault("refuge", {"status": "none", "level": 0})
    state.setdefault("class_selection", {"pending_users": []})

    for fid, faction in catalog.factions(pack).items():
        state["factions"].setdefault(fid, {
            "id": fid,
            "name": faction.get("name", fid),
            "disposition": faction.get("disposition", "neutral"),
            "power": int(faction.get("power", 1) or 1),
            "rep": 0,
            "goal": faction.get("goal", ""),
            "pressure": float(faction.get("pressure", 0.1) or 0.1),
        })

    for loc in state.get("world", {}).get("locations", []):
        lid = loc.get("id")
        if not lid:
            continue
        state["ecology"].setdefault(lid, {})
        if not state["ecology"][lid]:
            for mob_id, count in (loc.get("population") or {}).items():
                state["ecology"][lid][mob_id] = int(count)


def initial_locations(pack: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    ctx = gates.context(pack, state, None)
    result: list[dict[str, Any]] = []
    for lid, loc in catalog.worlds(pack).items():
        if not gates.check(loc.get("requires"), ctx):
            continue
        entry = copy.deepcopy(loc)
        entry["visited"] = bool(loc.get("start"))
        result.append(entry)
    return result


def refresh_locations(pack: dict[str, Any], state: dict[str, Any], member: dict[str, Any] | None = None) -> None:
    ctx = gates.context(pack, state, member)
    worlds = catalog.worlds(pack)
    current = {loc.get("id") for loc in state.get("world", {}).get("locations", [])}
    for lid, loc in worlds.items():
        if lid in current:
            continue
        if gates.check(loc.get("requires"), ctx):
            state.setdefault("world", {}).setdefault("locations", []).append(copy.deepcopy(loc))
    # Static geography can change between pack versions (names, coordinates,
    # descriptions, exits). Runtime fields such as visited stay untouched, so
    # existing campaigns receive the new Moscow map on their next read.
    static_keys = (
        "name", "desc", "era", "biome", "danger", "lat", "lon", "latitude",
        "longitude", "district", "coords", "floor", "population", "loot_table",
    )
    for loc in state.get("world", {}).get("locations", []):
        source = worlds.get(loc.get("id"))
        if not source:
            continue
        for key in static_keys:
            if key in source:
                loc[key] = copy.deepcopy(source[key])
        # Roads are merged, never replaced: a claimed refuge adds its own exit
        # to the anchor, and that runtime edge must survive a pack update.
        existing_exits = {str(edge.get("to")) for edge in (loc.get("exits") or [])}
        for edge in source.get("exits") or []:
            target = str(edge.get("to"))
            if target and target not in existing_exits:
                loc.setdefault("exits", []).append(copy.deepcopy(edge))
                existing_exits.add(target)
        # Objects keep their runtime state (taken, moved); only new props from
        # the pack are appended.
        known_objects = {str(obj.get("id")) for obj in (loc.get("objects") or [])}
        for obj in source.get("objects") or []:
            object_id = str(obj.get("id"))
            if object_id and object_id not in known_objects:
                loc.setdefault("objects", []).append(copy.deepcopy(obj))
                known_objects.add(object_id)
    for loc in state.get("world", {}).get("locations", []):
        state.setdefault("ecology", {}).setdefault(loc.get("id"), {})


def _location(state: dict[str, Any], loc_id: str | None) -> dict[str, Any] | None:
    if not loc_id:
        return None
    return next((loc for loc in state.get("world", {}).get("locations", []) if loc.get("id") == loc_id), None)


def _object_view(obj: dict[str, Any], index: int) -> dict[str, Any]:
    entry = copy.deepcopy(obj)
    entry.setdefault("distance_m", 1 + index)
    entry.setdefault("direction", DIRECTIONS[index % len(DIRECTIONS)])
    entry.setdefault("height_m", 1.0)
    entry.setdefault("kind", "object")
    entry.setdefault("alive", True)
    entry.setdefault("hostile", False)
    entry.setdefault("affordances", [])
    entry.setdefault("forbidden", [])
    return entry


def nearby_for(pack: dict[str, Any], state: dict[str, Any], member: dict[str, Any]) -> list[dict[str, Any]]:
    ensure_living(pack, state)
    character = member.get("character", {})
    loc_id = character.get("location") or state.get("world", {}).get("location", {}).get("id")
    loc = _location(state, loc_id)
    if not loc:
        return []
    result: list[dict[str, Any]] = []
    for index, obj in enumerate(loc.get("objects", [])):
        result.append(_object_view(obj, index))

    for exit_index, edge in enumerate(loc.get("exits", [])):
        target_id = edge.get("to")
        target = _location(state, target_id)
        via = edge.get("via") or (target or {}).get("name", target_id)
        result.append({
            "id": f"exit_{target_id}",
            "name": f"Выход: {via}",
            "aliases": [str(via), str((target or {}).get("name", ""))],
            "distance_m": 2 + exit_index,
            "direction": DIRECTIONS[(exit_index + 2) % len(DIRECTIONS)],
            "height_m": 1.0,
            "kind": "exit",
            "exit_to": target_id,
            "affordances": ["идти", "войти", "перейти"],
            "forbidden": [],
            "alive": True,
            "hostile": False,
        })

    ecology = state.get("ecology", {}).get(loc_id, {})
    slot = 0
    for mob_id, count in ecology.items():
        if count <= 0:
            continue
        data = catalog.mob(pack, mob_id) or {"id": mob_id, "name": mob_id, "level": 1}
        for _ in range(min(int(count), 3)):
            slot += 1
            result.append({
                "id": f"{mob_id}#{slot}",
                "name": data.get("name", mob_id),
                "aliases": [data.get("name", mob_id), mob_id] + list(data.get("aliases", [])),
                "distance_m": 6 + slot * 2,
                "direction": DIRECTIONS[(slot + 3) % len(DIRECTIONS)],
                "height_m": 1.8,
                "kind": "mob",
                "mob": mob_id,
                "level": int(data.get("level", 1) or 1),
                "alive": True,
                "hostile": True,
                "affordances": ["атаковать", "убежать", "спрятаться"],
                "forbidden": ["договориться"],
            })

    for npc_id, npc in state.get("npcs", {}).items():
        if not npc.get("alive", True) or npc.get("location") != loc_id:
            continue
        result.append({
            "id": npc_id,
            "name": npc.get("name", npc_id),
            "aliases": [npc.get("name", npc_id), npc.get("role", "")],
            "distance_m": 4,
            "direction": DIRECTIONS[len(result) % len(DIRECTIONS)],
            "height_m": 1.7,
            "kind": "npc",
            "npc_id": npc_id,
            "alive": True,
            "hostile": npc.get("disposition") in ("враждебен", "hostile"),
            "affordances": ["заговорить", "помочь", "торговать"],
            "forbidden": [],
            "activity": npc.get("activity", ""),
        })

    for other in state.get("members", []):
        if other.get("user_id") == member.get("user_id"):
            continue
        other_loc = other.get("character", {}).get("location")
        if other_loc != loc_id:
            continue
        result.append({
            "id": f"player_{other.get('user_id')}",
            "name": other.get("character", {}).get("name", "Спутник"),
            "aliases": [other.get("character", {}).get("name", "")],
            "distance_m": 2,
            "direction": "рядом",
            "height_m": 1.7,
            "kind": "party",
            "alive": other.get("character", {}).get("alive", True),
            "hostile": False,
            "affordances": ["заговорить", "помочь"],
            "forbidden": [],
        })

    result.sort(key=lambda item: (item.get("distance_m", 0), item.get("name", "")))
    return result


def find_nearby(state: dict[str, Any], member: dict[str, Any], target: str | None) -> dict[str, Any] | None:
    if not target:
        return None
    for item in nearby_for({}, state, member):
        if item.get("id") == target or target in item.get("aliases", []):
            return item
    return None


def _time_of_day(hour: int) -> str:
    if 5 <= hour < 9:
        return "рассвет"
    if 9 <= hour < 12:
        return "утро"
    if 12 <= hour < 17:
        return "день"
    if 17 <= hour < 21:
        return "вечер"
    return "ночь"


def _weather_for(season: str, hour: int, rng: random.Random) -> str:
    if season == "зима":
        return rng.choices(["clear", "overcast", "snow", "fog"], [30, 30, 30, 10])[0]
    if season == "лето":
        return rng.choices(["clear", "overcast", "rain", "storm"], [45, 25, 20, 10])[0]
    if season == "весна":
        return rng.choices(["clear", "overcast", "rain", "fog"], [30, 30, 25, 15])[0]
    return rng.choices(["clear", "overcast", "rain", "fog"], [25, 30, 25, 20])[0]


def _visibility(weather: str, hour: int) -> int:
    base = 60
    if hour < 5 or hour >= 22:
        base = 18
    elif hour < 7 or hour >= 19:
        base = 35
    if weather in ("fog",):
        base = max(8, base - 25)
    elif weather in ("rain", "storm", "snow"):
        base = max(12, base - 12)
    return base


def tick(pack: dict[str, Any], state: dict[str, Any], campaign_id: str,
         step_no: int) -> dict[str, Any]:
    ensure_living(pack, state)
    rng = rng_for(campaign_id, step_no, "world")
    world = state.setdefault("world", {})
    time_data = world.setdefault("time", {"day": 1, "hour": 8})
    time_data["hour"] = (int(time_data.get("hour", 8)) + 1) % 24
    if time_data["hour"] == 0:
        time_data["day"] = int(time_data.get("day", 1)) + 1

    living = state["living"]
    hour = int(time_data["hour"])
    day = int(time_data["day"])
    living["time_of_day"] = _time_of_day(hour)
    living["season"] = living.get("season", "осень")

    if day * 24 + hour >= int(living.get("weather_until", 0)):
        weather = _weather_for(str(living.get("season", "осень")), hour, rng)
        living["weather"] = weather
        living["weather_until"] = day * 24 + hour + rng.randint(3, 9)
    weather = str(living.get("weather", "clear"))
    world["weather"] = weather
    base_temp = {"зима": -6, "весна": 9, "лето": 24, "осень": 11}.get(str(living.get("season")), 11)
    temp = base_temp + (4 if 10 <= hour <= 17 else -4) + rng.randint(-3, 3)
    living["temperature_c"] = temp
    world["visibility_m"] = _visibility(weather, hour)

    facts: list[str] = []
    journal: list[str] = []
    rumors: list[str] = []

    npc_facts, npc_journal, npc_rumors = _tick_npcs(pack, state, rng)
    facts += npc_facts
    journal += npc_journal
    rumors += npc_rumors

    eco_facts = _tick_ecology(pack, state, rng)
    facts += eco_facts

    event = draw_event(pack, state, rng)
    if event:
        event_facts, event_journal, event_rumors = apply_event(pack, state, event, rng)
        facts += event_facts
        journal += event_journal
        rumors += event_rumors

    era_journal = advance_era(pack, state)
    journal += era_journal
    if era_journal:
        facts.append(f"Система объявляет новую эпоху: {state.get('era')}.")

    refresh_locations(pack, state, state.get("members", [{}])[0] if state.get("members") else None)

    living["events"] = (living.get("events", []) + facts)[-20:]
    living["rumors"] = (living.get("rumors", []) + rumors)[-30:]
    for text in journal:
        state.setdefault("_journal_pending", []).append(text)
    return {"facts": facts, "journal": journal, "rumors": rumors, "event": event}


def _tick_npcs(pack: dict[str, Any], state: dict[str, Any], rng: random.Random) -> tuple[list[str], list[str], list[str]]:
    facts: list[str] = []
    journal: list[str] = []
    rumors: list[str] = []
    hour = int(state.get("world", {}).get("time", {}).get("hour", 12))
    for npc_id, npc in list(state.get("npcs", {}).items()):
        if not npc.get("alive", True):
            continue
        schedule = npc.get("schedule", [])
        chosen = None
        for block in schedule:
            if int(block.get("from", 0)) <= hour <= int(block.get("to", 23)):
                chosen = block
                break
        if chosen:
            npc["location"] = chosen.get("location", npc.get("location"))
            npc["activity"] = chosen.get("activity", npc.get("activity", ""))

        needs = npc.setdefault("needs", {"hunger": 0, "fatigue": 0, "fear": 0})
        for key in ("hunger", "fatigue", "fear"):
            needs[key] = min(100, int(needs.get(key, 0)) + rng.randint(1, 4))
        if needs["hunger"] > 70 and npc.get("location") not in ("pharmacy", "school"):
            npc["location"] = rng.choice(["pharmacy", "school", "street"])
            npc["activity"] = "ищет еду"
        if needs["fatigue"] > 80:
            npc["activity"] = "отдыхает"

        memory = npc.setdefault("memory", [])
        if memory:
            npc["memory"] = memory[-8:]

        if npc.get("location") == state.get("world", {}).get("location", {}).get("id"):
            if npc.get("activity"):
                facts.append(f"{npc.get('name')} {npc.get('activity')}.")
        if needs["hunger"] > 90 and rng.random() < 0.15:
            rumors.append(f"{npc.get('name')} говорит, что еды в городе почти не осталось.")
            npc["memory"].append("голод")
    return facts, journal, rumors


def _mobs_for_biome(pack: dict[str, Any], biome: str, era: str) -> list[str]:
    current = gates.era_index(era)
    result: list[str] = []
    for mid, data in catalog.mobs(pack).items():
        if gates.era_index(data.get("era", "E0")) > current:
            continue
        tags = set(data.get("tags", []))
        if biome == "forest" and ("beast" in tags or "infected" in tags):
            result.append(mid)
        elif biome in ("urban", "road", "industrial") and ("humanoid" in tags or "human" in tags or "beast" in tags):
            result.append(mid)
        elif biome == "underground" and "infected" in tags:
            result.append(mid)
        elif biome == "ocean" and "ocean" in tags:
            result.append(mid)
        elif biome == "ark" and "ark" in tags:
            result.append(mid)
        elif biome == "techno" and "tech" in tags:
            result.append(mid)
        elif biome == "admin" and "admin" in tags:
            result.append(mid)
    return result


def _tick_ecology(pack: dict[str, Any], state: dict[str, Any], rng: random.Random) -> list[str]:
    facts: list[str] = []
    era = str(state.get("era", "E0"))
    for loc in state.get("world", {}).get("locations", []):
        lid = loc.get("id")
        if not lid or loc.get("safe"):
            continue
        ecology = state.setdefault("ecology", {}).setdefault(lid, {})
        cap = 1 + int(loc.get("danger", 1) or 1)
        total = sum(int(v) for v in ecology.values())
        if total > cap:
            for mob_id in list(ecology):
                if total <= cap:
                    break
                if ecology[mob_id] > 0:
                    ecology[mob_id] -= 1
                    total -= 1
        elif total < cap and rng.random() < 0.22:
            candidates = _mobs_for_biome(pack, str(loc.get("biome", "urban")), era)
            if candidates:
                mob_id = rng.choice(candidates)
                ecology[mob_id] = int(ecology.get(mob_id, 0)) + 1
                if lid == state.get("world", {}).get("location", {}).get("id"):
                    data = catalog.mob(pack, mob_id) or {}
                    facts.append(f"Где-то рядом появился {data.get('name', mob_id)}.")
    return facts


def draw_event(pack: dict[str, Any], state: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    ctx = gates.context(pack, state, (state.get("members") or [{}])[0])
    history = set(state.get("living", {}).get("event_history", [])[-4:])
    candidates = []
    for event in catalog.events(pack):
        if event.get("special"):
            continue
        if gates.era_index(event.get("era", "E0")) > gates.era_index(ctx["era"]):
            continue
        if event.get("id") in history:
            continue
        if not gates.check(event.get("requires"), ctx):
            continue
        weight = int(event.get("weight", 0) or 0)
        if weight > 0:
            candidates.append((event, weight))
    if not candidates:
        return None
    total = sum(w for _, w in candidates)
    roll = rng.randint(1, total)
    cursor = 0
    for event, weight in candidates:
        cursor += weight
        if roll <= cursor:
            state.setdefault("living", {}).setdefault("event_history", []).append(event.get("id"))
            return event
    return candidates[-1][0]


def apply_event(pack: dict[str, Any], state: dict[str, Any], event: dict[str, Any],
                rng: random.Random) -> tuple[list[str], list[str], list[str]]:
    ensure_living(pack, state)
    effects = event.get("effects", {}) or {}
    living = state["living"]
    facts: list[str] = []
    journal: list[str] = []
    rumors: list[str] = []

    if event.get("text"):
        facts.append(str(event["text"]))
    if event.get("journal"):
        journal.append(str(event["journal"]))
    if event.get("rumor"):
        rumors.append(str(event["rumor"]))

    if effects.get("weather"):
        living["weather"] = effects["weather"]
        state.setdefault("world", {})["weather"] = effects["weather"]
    if effects.get("visibility"):
        state.setdefault("world", {})["visibility_m"] = max(
            5, int(state.get("world", {}).get("visibility_m", 40)) + int(effects["visibility"]))
    if effects.get("temperature"):
        living["temperature_c"] = int(living.get("temperature_c", 12)) + int(effects["temperature"])
    if effects.get("knowledge"):
        for item in effects["knowledge"]:
            if item not in state.setdefault("knowledge", []):
                state["knowledge"].append(item)
                facts.append(f"Система добавляет знание: {item}.")
    if effects.get("condition"):
        for member in state.get("members", []):
            if member.get("status", "active") != "active":
                continue
            character = member.get("character", {})
            conditions = character.setdefault("conditions", [])
            cid = str(effects["condition"])
            if cid not in [c.get("id") if isinstance(c, dict) else c for c in conditions]:
                conditions.append({"id": cid, "name": cid})
    if effects.get("faction"):
        fid = str(effects["faction"].get("id", ""))
        delta = int(effects["faction"].get("delta", 0))
        if fid in state.get("factions", {}):
            state["factions"][fid]["rep"] = int(state["factions"][fid].get("rep", 0)) + delta
            facts.append(f"Те, кто держится «{state['factions'][fid].get('name', fid)}», стали смотреть иначе.")
    if effects.get("spawn"):
        spawn = effects["spawn"]
        loc = str(spawn.get("location", ""))
        mob_id = str(spawn.get("mob", ""))
        qty = int(spawn.get("qty", 1))
        if loc and mob_id:
            ecology = state.setdefault("ecology", {}).setdefault(loc, {})
            ecology[mob_id] = int(ecology.get(mob_id, 0)) + qty
    if effects.get("spawn_loot"):
        loc = str(effects["spawn_loot"])
        loc_data = _location(state, loc)
        if loc_data:
            loc_data["loot_bonus"] = int(loc_data.get("loot_bonus", 0)) + 1
    if effects.get("add_item"):
        item = effects["add_item"]
        state.setdefault("world", {}).setdefault("stash", []).append({
            "location": state.get("world", {}).get("location", {}).get("id"),
            "item": item.get("item"),
            "qty": int(item.get("qty", 1)),
        })
    if effects.get("add_npc"):
        npc = add_npc(pack, state, rng)
        if npc:
            facts.append(f"В городе появился человек: {npc['name']}.")
    if effects.get("class_selection"):
        for member in state.get("members", []):
            if member.get("status", "active") != "active" or not member.get("character", {}).get("alive", True):
                continue
            member.setdefault("character", {})["pending_class_selection"] = True
        facts.append("Система открыла выбор класса.")
    if effects.get("rest_bonus"):
        state["world"]["rest_bonus"] = True
    if effects.get("rumor"):
        rumors.append(str(event.get("rumor") or event.get("text", "")))
    return facts, journal, rumors


def add_npc(pack: dict[str, Any], state: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    used = {npc.get("name") for npc in state.get("npcs", {}).values()}
    names = [name for name in NPC_NAMES if name not in used]
    if not names:
        return None
    name = rng.choice(names)
    role, activity = rng.choice(NPC_ROLES)
    npc_id = f"npc_{len(state.setdefault('npcs', {})) + 1}"
    location = rng.choice([loc.get("id") for loc in state.get("world", {}).get("locations", [])
                           if loc.get("id") and not loc.get("spatial")])
    npc = {
        "id": npc_id,
        "name": name,
        "role": role,
        "kind": "human",
        "alive": True,
        "location": location,
        "home": location,
        "activity": activity,
        "disposition": "нейтрален",
        "faction": "survivors",
        "needs": {"hunger": rng.randint(10, 50), "fatigue": rng.randint(10, 50), "fear": rng.randint(10, 60)},
        "memory": [],
        "schedule": [
            {"from": 6, "to": 10, "location": location, "activity": "осматривается"},
            {"from": 11, "to": 15, "location": "street", "activity": "ищет припасы"},
            {"from": 16, "to": 21, "location": location, "activity": "возвращается к укрытию"},
            {"from": 22, "to": 5, "location": location, "activity": "отдыхает"},
        ],
    }
    state["npcs"][npc_id] = npc
    return npc


def advance_era(pack: dict[str, Any], state: dict[str, Any]) -> list[str]:
    current = str(state.get("era", "E0"))
    journal: list[str] = []
    member = (state.get("members") or [None])[0]
    ctx = gates.context(pack, state, member)
    for era_id, entry in catalog.unlocks(pack).items():
        if gates.era_index(era_id) <= gates.era_index(current):
            continue
        if not gates.check(entry.get("requires"), ctx):
            continue
        # only advance one era at a time
        state["era"] = era_id
        if entry.get("journal"):
            journal.append(str(entry["journal"]))
        if entry.get("rumor"):
            state.setdefault("living", {}).setdefault("rumors", []).append(str(entry["rumor"]))
        break
    return journal


def claim_refuge(pack: dict[str, Any], state: dict[str, Any], member: dict[str, Any]) -> dict[str, Any] | None:
    character = member.get("character", {})
    loc = _location(state, character.get("location"))
    if not loc or not loc.get("refuge_anchor"):
        return None
    anchor = loc["refuge_anchor"]
    state["refuge"] = {
        "status": "claimed",
        "type": anchor.get("type", "found"),
        "name": anchor.get("name", loc.get("name", "Убежище")),
        "level": int(anchor.get("level", 1) or 1),
        "anchor": loc.get("id"),
        "modules": [],
    }
    state.setdefault("flags", {})["refuge_claimed"] = True
    if "убежище" not in state.setdefault("knowledge", []):
        state["knowledge"].append("убежище")
    core = catalog.worlds(pack).get("refuge_core")
    if core and not any(l.get("id") == "refuge_core" for l in state.get("world", {}).get("locations", [])):
        entry = copy.deepcopy(core)
        entry["exits"] = [{"to": loc.get("id"), "via": "выход"}]
        state["world"]["locations"].append(entry)
        state["ecology"].setdefault("refuge_core", {})
    return state["refuge"]


def kill_mob(state: dict[str, Any], location_id: str | None, mob_id: str) -> bool:
    if not location_id:
        return False
    ecology = state.setdefault("ecology", {}).setdefault(location_id, {})
    if int(ecology.get(mob_id, 0)) <= 0:
        return False
    ecology[mob_id] = int(ecology[mob_id]) - 1
    return True


def draw_loot(pack: dict[str, Any], table_id: str, rng: random.Random) -> dict[str, Any]:
    table = catalog.loot_tables(pack).get(table_id)
    if not table:
        return {"items": [], "coins": 0, "xp": 0, "text": ""}
    entries = table.get("entries", [])
    total = sum(int(e.get("weight", 0)) for e in entries)
    if total <= 0:
        return {"items": [], "coins": 0, "xp": 0, "text": ""}
    roll = rng.randint(1, total)
    cursor = 0
    chosen = entries[-1]
    for entry in entries:
        cursor += int(entry.get("weight", 0))
        if roll <= cursor:
            chosen = entry
            break
    items = []
    if chosen.get("item"):
        items.append({"item": chosen["item"], "qty": int(chosen.get("qty", 1) or 1)})
    coins = 0
    if chosen.get("coins"):
        low, high = chosen["coins"]
        coins = rng.randint(int(low), int(high))
    xp = 0
    if chosen.get("xp"):
        low, high = chosen["xp"]
        xp = rng.randint(int(low), int(high))
    return {"items": items, "coins": coins, "xp": xp, "text": ""}


def current_location(state: dict[str, Any], member: dict[str, Any]) -> dict[str, Any] | None:
    return _location(state, member.get("character", {}).get("location"))
