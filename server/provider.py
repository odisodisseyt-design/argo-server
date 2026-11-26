from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
import weakref
from typing import Any

import httpx

from . import db
from .config import settings
from .models import ModelSpec

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

_digests: dict[str, str] = {}
_digests_at = 0.0


def clean(text: str) -> str:
    text = _THINK.sub("", text or "")
    text = _FENCE.sub("", text).strip()
    return text


def extract_json(text: str) -> Any:
    raw = clean(text)
    try:
        return json.loads(raw)
    except ValueError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except ValueError:
            pass
    return None


def _model_version(model: str, provider: str) -> str:
    if provider != "ollama":
        return model
    global _digests_at
    now = time.monotonic()
    if now - _digests_at > 60:
        _digests_at = now
        try:
            with httpx.Client(timeout=1.0) as client:
                response = client.get(f"{settings.ollama_base}/api/tags")
                if response.status_code == 200:
                    _digests.clear()
                    for item in response.json().get("models", []):
                        name = str(item.get("name", "")).split(":")[0]
                        digest = str(item.get("digest", ""))
                        if name and digest:
                            _digests[name] = digest
        except Exception:  # noqa: BLE001
            pass
    return _digests.get(model.split(":")[0], model)


_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def _mask_log(text: str, terms: tuple[str, ...] = ()) -> str:
    text = _EMAIL.sub("[email]", text or "")
    for term in terms:
        if term and len(term) > 2:
            text = text.replace(term, "[имя]")
    return text


class LLMProvider:
    async def complete(
        self,
        purpose: str,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        max_tokens: int = 300,
        spec: ModelSpec | None = None,
        timeout: float | None = None,
        campaign_id: str | None = None,
        step_no: int | None = None,
        mask_terms: tuple[str, ...] = (),
    ) -> str:
        request_id = uuid.uuid4().hex
        start = time.monotonic()
        prompt = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        provider = spec.provider if spec else settings.provider
        model = self._model_for(purpose, spec)
        limit = timeout if timeout is not None else self._timeout_for(purpose, spec)
        response_text = ""
        error = ""
        try:
            # httpx read timeouts reset on every keep-alive chunk, so a broken
            # provider can hang for minutes. The hard deadline below is the only
            # guarantee that a turn finishes before the proxy gives up.
            async with asyncio.timeout(max(1.0, float(limit))):
                if provider == "ollama":
                    response_text = await self._ollama(
                        messages, json_mode, max_tokens, model,
                        base=spec.base() if spec else "", timeout=limit,
                    )
                elif provider == "openai":
                    response_text = await self._openai(
                        messages, json_mode, max_tokens, model,
                        base=spec.base() if spec else "",
                        key=spec.key() if spec else settings.openai_key,
                        timeout=limit,
                    )
                elif provider == "cheapllm":
                    # The account allows exactly one running request. Serialize
                    # every campaign so co-op turns never trigger a 429 storm.
                    acquired = False
                    try:
                        await asyncio.wait_for(
                            _cheap_lock().acquire(), timeout=min(8.0, max(1.0, float(limit)))
                        )
                        acquired = True
                    except (asyncio.TimeoutError, TimeoutError):
                        raise TimeoutError("cheapllm busy")
                    try:
                        for attempt in range(2):
                            try:
                                response_text = await self._openai(
                                    messages, json_mode, max_tokens, model,
                                    base=settings.cheapllm_base, key=settings.cheapllm_key,
                                    timeout=limit,
                                )
                                break
                            except httpx.HTTPStatusError as exc:
                                code = exc.response.status_code
                                if code in (429, 503) and attempt == 0:
                                    retry = exc.response.headers.get("retry-after", "")
                                    try:
                                        delay = float(retry)
                                    except (TypeError, ValueError):
                                        delay = 2.0
                                    await asyncio.sleep(min(6.0, max(1.0, delay)))
                                    continue
                                raise
                    finally:
                        _cheap_lock().release()
                elif provider == "gemini":
                    response_text = await self._gemini(
                        messages, json_mode, max_tokens, model,
                        key=spec.key() if spec else settings.gemini_key, timeout=limit,
                    )
                else:
                    raise RuntimeError(f"unknown provider {provider}")
        except (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError):
            error = "timeout: hard-deadline"
            response_text = ""
        except Exception as exc:  # noqa: BLE001
            error = _mask_log(f"{type(exc).__name__}: {exc}")
            error = re.sub(r"(?i)(api[_-]?key|key|token)=[^&\s]+", r"\1=***", error)
            response_text = ""
        latency = int((time.monotonic() - start) * 1000)
        await db.execute(
            """INSERT INTO llm_logs(request_id,campaign_id,step_no,purpose,provider,model,
                                    model_version,prompt,response,latency_ms,error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (request_id, campaign_id, step_no, purpose, provider, model,
             _model_version(model, provider), _mask_log(prompt, mask_terms),
             _mask_log(clean(response_text), mask_terms), latency, error),
        )
        return clean(response_text)

    def _model_for(self, purpose: str, spec: ModelSpec | None = None) -> str:
        if spec:
            return spec.model_for(purpose)
        if purpose == "intent" and settings.intent_model:
            return settings.intent_model
        if purpose == "narrator" and settings.narrator_model:
            return settings.narrator_model
        if settings.provider == "gemini":
            return settings.gemini_model
        return settings.model

    def _timeout_for(self, purpose: str, spec: ModelSpec | None = None) -> float:
        if spec:
            return spec.timeout_for(purpose)
        return settings.intent_timeout if purpose == "intent" else settings.narrator_timeout

    @property
    def model_name(self) -> str:
        return self._model_for("narrator")

    async def _ollama(self, messages: list[dict[str, str]], json_mode: bool,
                      max_tokens: int, model: str, base: str = "",
                      timeout: float | None = None) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {"temperature": settings.temperature if not json_mode else 0.1,
                        "num_ctx": settings.num_ctx, "num_predict": max_tokens,
                        "stop": ["КРАТКАЯ ПАМЯТЬ", "ПРОИЗОШЛО:", "РАНЕЕ:",
                                 "СЛОВА ИГРОКА", "entities_used"]},
        }
        if json_mode:
            payload["format"] = "json"
        async with httpx.AsyncClient(timeout=timeout or settings.request_timeout) as client:
            r = await client.post(f"{base or settings.ollama_base}/api/chat", json=payload)
            r.raise_for_status()
            return r.json().get("message", {}).get("content", "")

    async def _openai(self, messages: list[dict[str, str]], json_mode: bool,
                      max_tokens: int, model: str, base: str = "", key: str = "",
                      timeout: float | None = None) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": settings.temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {key or settings.openai_key}"}
        async with httpx.AsyncClient(timeout=timeout or settings.request_timeout) as client:
            r = await client.post(
                f"{base or settings.openai_base}/chat/completions",
                json=payload, headers=headers,
            )
            if r.status_code == 400 and "response_format" in payload:
                # Some OpenAI-compatible gateways reject JSON mode. Retry once
                # without it instead of losing the whole turn.
                payload.pop("response_format", None)
                r = await client.post(
                    f"{base or settings.openai_base}/chat/completions",
                    json=payload, headers=headers,
                )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict) or "choices" not in data:
                reason = ""
                if isinstance(data, dict):
                    err = data.get("error")
                    reason = str(err) if err else ",".join(list(data)[:5])
                raise RuntimeError(f"provider response without choices: {reason[:160]}")
            return data["choices"][0]["message"].get("content", "")

    async def _gemini(self, messages: list[dict[str, str]], json_mode: bool,
                      max_tokens: int, model: str, key: str = "",
                      timeout: float | None = None) -> str:
        contents = [{"role": "user" if m["role"] != "assistant" else "model",
                     "parts": [{"text": m["content"]}]} for m in messages]
        config: dict[str, Any] = {"temperature": settings.temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            config["responseMimeType"] = "application/json"
        async with httpx.AsyncClient(timeout=timeout or settings.request_timeout) as client:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key or settings.gemini_key},
                json={"contents": contents, "generationConfig": config},
            )
            r.raise_for_status()
            data = r.json()
            return "".join(p.get("text", "") for c in data.get("candidates", [])
                           for p in c.get("content", {}).get("parts", []))


_CHEAP_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _cheap_lock() -> asyncio.Semaphore:
    """One lock per event loop: a module-level semaphore binds to the first loop."""
    loop = asyncio.get_running_loop()
    lock = _CHEAP_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Semaphore(1)
        _CHEAP_LOCKS[loop] = lock
    return lock
provider = LLMProvider()


INTENT_SYSTEM = """Выбери один id действия из ACTIONS и, если нужно, id цели из NEARBY.
Ответ — только JSON: {"action_id":"id","target":"id или null","method_tags":[],"confidence":0.9,"clarifying_question":null}
Не выдумывай id. Если непонятно, confidence<0.6 и короткий вопрос в мире на __LANGUAGE__.
ACTIONS: __ACTIONS__
NEARBY: __NEARBY__"""

NARRATOR_SYSTEM = """Ты — рассказчик русской текстовой RPG. Пиши только по-русски.
Ответь только JSON: {"prose":"...","entities_used":["token",...]}.

Ты продолжаешь начатую сцену, а не начинаешь новую. Сначала прочитай РАНЕЕ и КРАТКУЮ ПАМЯТЬ МИРА, затем напиши 3–5 ярких предложений, которые прямо продолжают последнюю фразу.
Никогда не повторяй предыдущий текст дословно. Не используй те же обороты из РАНЕЕ; находи свежие слова. Не начинай с пересказа.

Правила:
- Используй только людей, предметы, места, раны и факты из ПРОИЗОШЛО, РАНЕЕ, КРАТКОЙ ПАМЯТИ МИРА или ДОСТУПНО. Ничего не выдумывай.
- Если игрок задал вопрос или что-то сказал — ответь по-мировому, опираясь только на известные факты.
- Не говори от лица игрока. Не используй слова «игрок», id, числа, проценты, игровые термины, системные сообщения, меню и пояснения от автора.
- Конкретные звуки, запахи, свет, прикосновения — только из разрешённых фактов.
- Опиши то, что изменилось за этот шаг. Не переписывай одно и то же место каждый раз в одном порядке.
- Если ничего не произошло — одна короткая фраза о том, что физически присутствует.
- entities_used содержит только токены из ДОСТУПНО.

СТИЛЬ: __STYLE__
ДОСТУПНО: __ALLOWED__"""

NARRATOR_SYSTEM_LOCAL = """Ты — рассказчик русской текстовой RPG. Опиши один ход героя.
Пиши только по-русски, обращайся к герою на «ты», в прошедшем времени. Ровно два коротких предложения.
Первое предложение — что изменилось. Второе — одна деталь обстановки: звук, свет, холод, запах.
Бери только то, что есть в ПРОИЗОШЛО, РАНЕЕ и ДОСТУПНО. Не придумывай людей, предметов, мест и действий.
Если в ПРОИЗОШЛО нет ясного изменения, опиши само действие из СЛОВА ИГРОКА.
Не повторяй строки из ПРОИЗОШЛО и РАНЕЕ дословно. Не начинай предложения с «Вы».
Без JSON, кавычек, звёздочек, списков и подписей. Без игровых слов: уровень, опыт, урон, система, игрок.
Избегай клише. Не используй слова: словно, будто, как будто, внезапно, вдруг.
ТОН: __STYLE__
ДОСТУПНО: __ALLOWED__"""

SUMMARY_SYSTEM = """You keep a compact memory of a text RPG. Write in __LANGUAGE__.
Return JSON only: {"prose":"2 sentences summarizing the current scene for continuity"}.
Use only facts from the provided text. Do not invent anything.
STYLE: __STYLE__"""
