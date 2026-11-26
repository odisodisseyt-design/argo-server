from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="tw-tests-"))
os.environ["TW_DB_PATH"] = str(_TMP / "world.db")
os.environ["TW_REQUIRE_IDEMPOTENCY"] = "0"

import pytest  # noqa: E402
import httpx  # noqa: E402

from server import auth, db, limits, models  # noqa: E402
from server.provider import provider  # noqa: E402
from server.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def no_ollama_probe(monkeypatch):
    monkeypatch.setattr(models, "_check_availability", lambda: None)


@pytest.fixture(autouse=True)
def no_background_resolution(monkeypatch):
    # HTTP tests must not leave resolution tasks running after the loop closes.
    from server import main
    monkeypatch.setattr(main, "_schedule_resolution", lambda campaign_id, step_no: None)


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    async def complete(purpose, messages, *args, **kwargs):
        if purpose == "intent":
            return '{"action_id":"","confidence":0}'
        return '{"prose":"Он осмотрелся вокруг.","entities_used":[]}'

    monkeypatch.setattr(provider, "complete", complete)


@pytest.fixture(autouse=True)
async def clean_db():
    await db.init_db()
    for table in ("turn_idempotency", "rate_limits", "orders", "steps", "ledger",
                  "campaign_members", "campaigns", "llm_logs", "audit_logs",
                  "tokens", "users", "schema_migrations"):
        await db.execute(f"DELETE FROM {table}")
    models.invalidate()
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def player():
    user = await auth.register("player_test", "password123")
    token = await auth.login("player_test", "password123")
    return {"id": user["id"], "username": user["username"], "token": token,
            "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
async def second_player():
    user = await auth.register("player_two", "password123")
    token = await auth.login("player_two", "password123")
    return {"id": user["id"], "username": user["username"], "token": token,
            "headers": {"Authorization": f"Bearer {token}"}}
