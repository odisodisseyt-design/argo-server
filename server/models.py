"""Model catalog visible to the player through /api/models.

Only public metadata leaves the server: id, title, strength, speed, cost,
recommended_for, available. Provider internals and API keys never do.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings

_cache: dict[str, ModelSpec] | None = None
_availability_checked = 0.0


@dataclass(slots=True)
class ModelSpec:
    id: str
    title: str
    strength: int
    speed: int
    cost: str
    recommended_for: str
    provider: str
    intent_model: str
    narrator_model: str
    intent_timeout: float
    narrator_timeout: float
    narrator_max_tokens: int = 100
    base_env: str = ""
    key_env: str = ""
    available: bool = True

    def model_for(self, purpose: str) -> str:
        return self.intent_model if purpose == "intent" else self.narrator_model

    def timeout_for(self, purpose: str) -> float:
        return self.intent_timeout if purpose == "intent" else self.narrator_timeout

    def base(self) -> str:
        return os.getenv(self.base_env, "") if self.base_env else ""

    def key(self) -> str:
        return os.getenv(self.key_env, "") if self.key_env else ""

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "strength": self.strength,
            "speed": self.speed,
            "cost": self.cost,
            "recommended_for": self.recommended_for,
            "available": self.available,
        }


def _load() -> dict[str, ModelSpec]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(settings.models_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = []
    result: dict[str, ModelSpec] = {}
    for entry in raw:
        model_id = str(entry.get("id", ""))
        if not model_id or model_id in result:
            continue
        result[model_id] = ModelSpec(
            id=model_id,
            title=str(entry.get("title", model_id)),
            strength=max(1, min(5, int(entry.get("strength", 3) or 3))),
            speed=max(1, min(5, int(entry.get("speed", 3) or 3))),
            cost=str(entry.get("cost", "free")),
            recommended_for=str(entry.get("recommended_for", "")),
            provider=str(entry.get("provider", settings.provider)),
            intent_model=str(entry.get("intent_model", settings.intent_model)),
            narrator_model=str(entry.get("narrator_model", settings.narrator_model)),
            intent_timeout=float(entry.get("intent_timeout", settings.intent_timeout)),
            narrator_timeout=float(entry.get("narrator_timeout", settings.narrator_timeout)),
            narrator_max_tokens=max(60, min(400, int(entry.get("narrator_max_tokens", 100) or 100))),
            base_env=str(entry.get("base_env", "")),
            key_env=str(entry.get("key_env", "")),
            available=bool(entry.get("available", True)),
        )
    _cache = result
    return result


def all_models() -> list[ModelSpec]:
    _check_availability()
    return list(_load().values())


def get(model_id: str | None) -> ModelSpec:
    specs = _load()
    spec = specs.get(model_id or "")
    if spec is None:
        spec = specs.get(settings.default_model_id)
    if spec is None:
        spec = next(iter(specs.values()), None)
    if spec is None:
        spec = ModelSpec(
            id="fast",
            title="Быстрая",
            strength=1,
            speed=5,
            cost="free",
            recommended_for="",
            provider=settings.provider,
            intent_model=settings.intent_model,
            narrator_model=settings.narrator_model,
            intent_timeout=settings.intent_timeout,
            narrator_timeout=settings.narrator_timeout,
        )
    _check_availability()
    return spec


def visible() -> list[dict[str, Any]]:
    return [spec.view() for spec in all_models()]


def validate(model_id: str) -> bool:
    spec = _load().get(model_id)
    return bool(spec and spec.available)


def invalidate() -> None:
    global _cache, _availability_checked
    _cache = None
    _availability_checked = 0.0


def _check_availability() -> None:
    global _availability_checked
    now = time.monotonic()
    if now - _availability_checked < 60:
        return
    _availability_checked = now
    specs = _load()
    needs_ollama = any(s.provider == "ollama" for s in specs.values())
    installed: set[str] = set()
    if needs_ollama:
        try:
            with httpx.Client(timeout=1.0) as client:
                response = client.get(f"{settings.ollama_base}/api/tags")
                if response.status_code == 200:
                    installed = {
                        str(item.get("name", "")).split(":")[0]
                        for item in response.json().get("models", [])
                    }
        except Exception:  # noqa: BLE001
            installed = set()
    for spec in specs.values():
        if spec.provider == "ollama" and installed:
            names = {spec.intent_model.split(":")[0], spec.narrator_model.split(":")[0]}
            spec.available = bool(names & installed)
        elif spec.provider == "cheapllm":
            spec.available = bool(settings.cheapllm_key)
        elif spec.provider in ("openai", "gemini"):
            spec.available = bool(spec.key() or settings.openai_key or settings.gemini_key)
