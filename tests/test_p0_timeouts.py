import httpx

from server import db, engine
from server.packs import load_pack
from server.provider import LLMProvider, provider


def _real_complete(monkeypatch):
    monkeypatch.setattr(provider, "complete", LLMProvider.complete.__get__(provider))


async def test_intent_timeout_uses_loose_deterministic(player, monkeypatch):
    async def slow(*args, **kwargs):
        raise httpx.TimeoutException("slow model")

    _real_complete(monkeypatch)
    monkeypatch.setattr(provider, "_ollama", slow)
    pack = load_pack("refuge")
    state = {"world": {"locations": []}}
    member = {"user_id": player["id"], "character": {"location": "apartment"}}
    order = await engine.parse_intent(pack, "оглянуться спрятаться", state, member)
    assert order["action_id"] in pack["actions"]
    assert order["confidence"] == 0.95
    logs = await db.fetchall("SELECT * FROM llm_logs WHERE purpose='intent'")
    assert logs and logs[-1]["error"].startswith("timeout")


async def test_narrator_timeout_falls_back_to_facts(player, monkeypatch):
    async def slow(*args, **kwargs):
        raise httpx.TimeoutException("slow model")

    _real_complete(monkeypatch)
    monkeypatch.setattr(provider, "_ollama", slow)
    campaign = await engine.create_campaign("refuge", "Тест", player, [], model_id="fast")
    await engine.submit_order(campaign, player["id"], "Обыскать стол")
    row = await db.fetchone(
        "SELECT result_json FROM steps WHERE campaign_id=?", (campaign,)
    )
    payload = db.loads(row["result_json"], {})
    prose = payload["narrations"][str(player["id"])]
    assert prose and "Ты находишь" in prose
    logs = await db.fetchall("SELECT * FROM llm_logs WHERE purpose='narrator'")
    assert logs and logs[-1]["error"].startswith("timeout")
