"""Обёртка над AsyncOpenAI: стриминг (с function calling), изображения, список моделей, ретраи."""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from config import settings

log = logging.getLogger(__name__)

MAX_RETRIES = 3  # + 1 основная попытка
MODELS_TTL = 300.0
TOOLS_OFF_SECONDS = 600.0

_client = AsyncOpenAI(
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url,
    timeout=settings.request_timeout,
    max_retries=0,  # повторы делаем сами: понятные сообщения и никаких повторов после начала ответа
)

_models_cache: tuple[float, list[str]] = (0.0, [])
_tools_off_until = 0.0

StreamEvent = tuple[str, Any]  # ("text", str) | ("tool_calls", [{"id","name","arguments"}])


class LLMError(Exception):
    """Ошибка, текст которой безопасно показать пользователю."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def classify(exc: Exception, model: str = "") -> tuple[bool, str]:
    """→ (стоит ли повторять, сообщение для пользователя)."""
    if isinstance(exc, openai.AuthenticationError):
        return False, "Ошибка авторизации у провайдера (401). Сообщите администратору бота."
    if isinstance(exc, openai.RateLimitError):
        return True, "Провайдер перегружен или превышен лимит (429). Попробуйте чуть позже."
    if isinstance(exc, openai.APITimeoutError):
        return True, "Провайдер слишком долго не отвечает (таймаут). Попробуйте ещё раз."
    if isinstance(exc, openai.APIConnectionError):
        return True, "Не удалось связаться с провайдером. Попробуйте ещё раз."
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return True, f"Ошибка на стороне провайдера ({exc.status_code}). Попробуйте позже."
        if exc.status_code == 404:
            return False, f"Модель «{model}» недоступна у провайдера. Выберите другую: /model."
        return False, f"Запрос отклонён провайдером ({exc.status_code})."
    return False, "Непредвиденная ошибка при обращении к модели."


async def _sleep_backoff(attempt: int) -> None:
    await asyncio.sleep(2**attempt + random.uniform(0, 0.5))  # 1с, 2с, 4с (+ джиттер)


async def stream_chat(
    messages: list[dict[str, Any]],
    *,
    model: str,
    temperature: float,
    max_tokens: int | None,
    tools: list[dict[str, Any]] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Отдаёт события ("text", дельта) и в конце, если модель вызвала функции, ("tool_calls", [...]).

    Повторяет запрос только пока ничего не отдано. Если провайдер отверг `tools` (400/422),
    запрос автоматически повторяется без них, а инструменты отключаются на 10 минут.
    """
    global _tools_off_until
    for attempt in range(MAX_RETRIES + 1):
        use_tools = bool(tools) and time.monotonic() >= _tools_off_until
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            params["max_tokens"] = max_tokens
        if use_tools:
            params["tools"] = tools

        started = False
        calls: dict[int, dict[str, str]] = {}
        try:
            stream = await _client.chat.completions.create(**params)
            async with stream:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    text = getattr(delta, "content", None)
                    if text:
                        started = True
                        yield ("text", text)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        started = True
                        idx = tc.index if tc.index is not None else 0
                        slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if fn.name and not slot["name"]:
                                slot["name"] = fn.name
                            if fn.arguments:
                                slot["arguments"] += fn.arguments
            if calls:
                yield (
                    "tool_calls",
                    [{**calls[i], "id": calls[i]["id"] or f"call_{i}"} for i in sorted(calls)],
                )
            return
        except Exception as exc:  # noqa: BLE001 — CancelledError/GeneratorExit сюда не попадают
            if use_tools and not started and isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
                log.warning("Провайдер не принял tools (%s): работаю без инструментов %d мин.", exc, TOOLS_OFF_SECONDS // 60)
                _tools_off_until = time.monotonic() + TOOLS_OFF_SECONDS
                continue
            retryable, message = classify(exc, model)
            if retryable and not started and attempt < MAX_RETRIES:
                log.warning("LLM error (%s), retry %d/%d", type(exc).__name__, attempt + 1, MAX_RETRIES)
                await _sleep_backoff(attempt)
                continue
            log.error("LLM request failed: %s: %s", type(exc).__name__, exc)
            raise LLMError(message) from exc


# ───────────────────────── изображения ─────────────────────────


def _sizes() -> dict[str, str]:
    if settings.image_model.lower().startswith("dall-e"):
        return {"square": "1024x1024", "landscape": "1792x1024", "portrait": "1024x1792"}
    return {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}


def _image_error(exc: Exception) -> LLMError:
    if isinstance(exc, (openai.NotFoundError, openai.BadRequestError, openai.UnprocessableEntityError)):
        log.error("Image API rejected request: %s", exc)
        return LLMError(
            f"Провайдер не принял запрос к изображениям (модель «{settings.image_model}»). "
            "Возможно, он не поддерживает генерацию картинок — проверьте IMAGE_MODEL в .env."
        )
    log.error("Image request failed: %s: %s", type(exc).__name__, exc)
    return LLMError(classify(exc, settings.image_model)[1])


async def _image_bytes(item: Any) -> bytes:
    if getattr(item, "b64_json", None):
        return base64.b64decode(item.b64_json)
    if getattr(item, "url", None):
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
            resp = await http.get(item.url)
            resp.raise_for_status()
            return resp.content
    raise LLMError("Провайдер не вернул изображение.")


async def generate_image(prompt: str, aspect: str = "square") -> bytes:
    sizes = _sizes()
    try:
        resp = await _client.images.generate(
            model=settings.image_model, prompt=prompt, size=sizes.get(aspect, sizes["square"]), n=1
        )
        return await _image_bytes(resp.data[0])
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _image_error(exc) from exc


async def edit_image(data: bytes, filename: str, prompt: str) -> bytes:
    mime = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else (
        "image/webp" if filename.lower().endswith(".webp") else "image/png"
    )
    try:
        resp = await _client.images.edit(model=settings.image_model, image=(filename, data, mime), prompt=prompt)
        return await _image_bytes(resp.data[0])
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _image_error(exc) from exc


# ───────────────────────── модели ─────────────────────────


async def list_models() -> list[str]:
    """GET /v1/models с кэшем. При ошибке — старый кэш или модель по умолчанию."""
    global _models_cache
    ts, cached = _models_cache
    if cached and time.monotonic() - ts < MODELS_TTL:
        return cached

    for attempt in range(MAX_RETRIES + 1):
        try:
            page = await _client.models.list()
            ids = sorted({m.id for m in page.data}) or [settings.openai_model]
            if settings.openai_model not in ids:
                log.warning("OPENAI_MODEL=%s нет в /models провайдера. Доступны: %s", settings.openai_model, ", ".join(ids))
            _models_cache = (time.monotonic(), ids)
            return ids
        except Exception as exc:  # noqa: BLE001
            retryable, _ = classify(exc)
            if retryable and attempt < MAX_RETRIES:
                await _sleep_backoff(attempt)
                continue
            log.error("Cannot fetch models: %s: %s", type(exc).__name__, exc)
            break
    return cached or [settings.openai_model]
