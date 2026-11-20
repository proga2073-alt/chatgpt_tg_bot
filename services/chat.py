"""Общая логика чата: допуск запроса, подготовка контекста, списание ⭐, сохранение ответа."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from config import settings
from db import repo
from db.models import DEFAULT_TITLE, Message
from db.session import SessionLocal
from services import billing
from services.ratelimit import SlidingWindowLimiter

log = logging.getLogger(__name__)

_chat_limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)
_active: dict[int, float] = {}  # user_id → момент начала генерации
ACTIVE_TTL = 600.0  # «зависший» флаг генерации сам протухает
cancel_events: dict[int, asyncio.Event] = {}  # кнопка «Стоп»


class RequestRejected(Exception):
    """Запрос не допущен (антиспам / лимит). `message` можно показывать пользователю."""

    def __init__(self, message: str, retry_after: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class PaymentRequired(Exception):
    """Для выбранной модели не хватает баланса."""

    def __init__(self, model: str, cost: int, balance: int) -> None:
        self.model, self.cost, self.balance = model, cost, balance
        self.text = (
            f"Модель «{model}» платная: {billing.fmt_stars(cost)} за сообщение, "
            f"на балансе {billing.fmt_stars(balance)}. "
            "Пополните баланс (/buy) или выберите бесплатную модель (/model)."
        )
        super().__init__(self.text)


@dataclass(slots=True)
class Turn:
    conversation_id: int
    title: str
    messages: list[dict[str, str]]
    model: str
    temperature: float
    max_tokens: int | None
    cost: int = 0  # списано кредитов (для возврата, если ответа не получилось)


async def admit(user_id: int) -> None:
    """Антиспам: одна генерация за раз, N запросов в минуту, необязательный дневной лимит.

    После успешного admit() обязательно вызвать release().
    """
    now = time.monotonic()
    started = _active.get(user_id)
    if started is not None and now - started < ACTIVE_TTL:
        raise RequestRejected("Подождите, предыдущий ответ ещё генерируется.", retry_after=2)

    wait = _chat_limiter.hit(user_id)
    if wait > 0:
        sec = math.ceil(wait)
        raise RequestRejected(f"Слишком много запросов. Подождите {sec} с.", retry_after=sec)

    _active[user_id] = now  # ставим до await, чтобы два параллельных запроса не прошли оба
    if settings.daily_limit:
        try:
            async with SessionLocal() as s:
                ok = await repo.bump_usage(s, user_id, settings.daily_limit)
                await s.commit()
        except BaseException:
            _active.pop(user_id, None)
            raise
        if not ok:
            _active.pop(user_id, None)
            raise RequestRejected(f"Достигнут дневной лимит ({settings.daily_limit} запросов). Приходите завтра.")


def release(user_id: int) -> None:
    _active.pop(user_id, None)


def build_messages(system_prompt: str, history: list[Message]) -> list[dict[str, str]]:
    """Скользящее окно: последние N сообщений, дополнительно обрезанные по суммарной длине."""
    window = history[-settings.history_max_messages :]
    kept: list[Message] = []
    total = 0
    for m in reversed(window):
        total += len(m.content)
        if kept and total > settings.history_max_chars:
            break
        kept.append(m)
    kept.reverse()
    out: list[dict[str, str]] = []
    if system_prompt.strip():
        out.append({"role": "system", "content": system_prompt})
    out.extend({"role": m.role, "content": m.content} for m in kept)
    return out


async def new_conversation(user_id: int, username: str | None) -> int:
    async with SessionLocal() as s:
        user = await repo.get_or_create_user(s, user_id, username)
        conv = await repo.create_conversation(s, user_id)
        user.current_conversation_id = conv.id
        await s.commit()
        return conv.id


async def start_turn(user_id: int, username: str | None, text: str | None, *, regenerate: bool = False) -> Turn:
    """Списывает ⭐, сохраняет сообщение пользователя и собирает контекст.

    regenerate=True: удаляет последний ответ и повторяет запрос по последнему сообщению пользователя
    (так же работает «Повторить» после ошибки). Нечего повторять → LookupError.
    """
    async with SessionLocal() as s:
        user = await repo.get_or_create_user(s, user_id, username)
        conv = None
        if user.current_conversation_id is not None:
            conv = await repo.get_conversation(s, user_id, user.current_conversation_id)
        if conv is None:
            if regenerate:
                raise LookupError("nothing to regenerate")
            conv = await repo.create_conversation(s, user_id)

        model = user.model or settings.openai_model
        if not billing.is_enabled(model):  # администратор скрыл модель → используем модель по умолчанию
            model = settings.openai_model
        cost = billing.cost_credits(model)
        if cost and not await repo.charge(s, user_id, cost):  # списание и сообщение — в одной транзакции
            raise PaymentRequired(model, cost, await repo.get_balance(s, user_id))

        if regenerate:
            await repo.delete_last_assistant(s, conv.id)
            last = await repo.recent_messages(s, conv.id, 1)
            if not last or last[0].role != "user":
                raise LookupError("nothing to regenerate")
        else:
            assert text is not None
            if conv.title == DEFAULT_TITLE:
                conv.title = (" ".join(text.split()))[:40] or DEFAULT_TITLE
            await repo.add_message(s, conv.id, "user", text)
        user.current_conversation_id = conv.id

        history = await repo.recent_messages(s, conv.id, settings.history_max_messages)
        turn = Turn(
            conversation_id=conv.id,
            title=conv.title,
            messages=build_messages(user.system_prompt or settings.default_system_prompt, history),
            model=model,
            temperature=user.temperature,
            max_tokens=user.max_tokens,
            cost=cost,
        )
        await s.commit()
        return turn


async def finish_turn(conversation_id: int, answer: str) -> None:
    async with SessionLocal() as s:
        await repo.add_message(s, conversation_id, "assistant", answer)
        await s.commit()


async def refund(user_id: int, credits: int) -> None:
    """Возвращает кредиты (модель не дала результата)."""
    if credits <= 0:
        return
    async with SessionLocal() as s:
        await repo.add_credits(s, user_id, credits)
        await s.commit()
