from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import PACKS_DIR

# Moscow frame used for every campaign point. The basemap in the pack is
# generated in exactly this frame, so the client never has to know the
# projection details.
MOSCOW_BBOX = (55.55, 37.30, 55.95, 37.95)
MAP_WIDTH = 1000.0

_BIOME_NAMES = {
    "urban": "Город", "road": "Дорога", "forest": "Лес", "underground": "Подземье",
    "industrial": "Промзона", "ocean": "Вода", "techno": "Металл",
    "ark": "Ковчег", "admin": "Исток", "refuge": "Убежище",
}


def _mercator(lat: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


_LAT0, _LON0, _LAT1, _LON1 = MOSCOW_BBOX
_SPAN_X = math.radians(_LON1 - _LON0)
_SPAN_Y = _mercator(_LAT1) - _mercator(_LAT0)
MAP_HEIGHT = round(MAP_WIDTH * _SPAN_Y / _SPAN_X, 1)


def project(lat: float, lon: float) -> tuple[float, float]:
    """Project a real WGS84 point into the basemap frame (0..MAP_WIDTH, 0..MAP_HEIGHT)."""
    x = (math.radians(lon - _LON0) / _SPAN_X) * MAP_WIDTH
    y = ((_mercator(_LAT1) - _mercator(lat)) / _SPAN_Y) * MAP_HEIGHT
    return round(x, 1), round(y, 1)


def biome_name(value: Any) -> str:
    return _BIOME_NAMES.get(str(value or ""), "Неизвестно")


def danger_name(value: Any) -> str:
    try:
        level = int(value)
    except (TypeError, ValueError):
        return "Неизвестно"
    if level <= 1:
        return "Спокойно"
    if level <= 3:
        return "Осторожно"
    if level <= 6:
        return "Опасно"
    if level <= 9:
        return "Очень опасно"
    return "Смертельно"


def location_point(location: dict[str, Any], index: int = 0) -> tuple[float, float]:
    lat = location.get("lat", location.get("latitude"))
    lon = location.get("lon", location.get("longitude"))
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return project(float(lat), float(lon))
    coords = location.get("coords") or []
    x = float(coords[0]) if len(coords) > 0 else float((index % 6) * 60 + 40)
    y = float(coords[1]) if len(coords) > 1 else float((index // 6) * 60 + 40)
    return x, y


def location_view(location: dict[str, Any], index: int = 0) -> dict[str, Any]:
    """Player-facing location: no population, loot tables or internal ids."""
    x, y = location_point(location, index)
    lat = location.get("lat", location.get("latitude"))
    lon = location.get("lon", location.get("longitude"))
    return {
        "id": str(location.get("id", "")),
        "name": str(location.get("name", "")),
        "district": str(location.get("district", "Москва")),
        "lat": float(lat) if isinstance(lat, (int, float)) else None,
        "lon": float(lon) if isinstance(lon, (int, float)) else None,
        "x": x,
        "y": y,
        "biome": biome_name(location.get("biome")),
        "danger": danger_name(location.get("danger", 0)),
    }


@lru_cache(maxsize=4)
def load_basemap(pack_id: str) -> dict[str, Any] | None:
    path: Path = PACKS_DIR / pack_id / "map.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("roads"):
        return None
    data.setdefault("id", "moscow")
    data.setdefault("attribution", "© OpenStreetMap contributors")
    data.setdefault("width", MAP_WIDTH)
    data.setdefault("height", MAP_HEIGHT)
    return data


def invalidate_basemap() -> None:
    load_basemap.cache_clear()
