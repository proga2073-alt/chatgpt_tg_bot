from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject

from config import settings
from db import repo
from db.session import SessionLocal


class GuardMiddleware(BaseMiddleware):
    """Регистрирует пользователя и не пускает заблокированных администратором."""

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)
        async with SessionLocal() as s:
            user = await repo.get_or_create_user(s, tg_user.id, tg_user.username)
            await s.commit()
        if user.banned and tg_user.id not in settings.admin_set:
            if isinstance(event, Message):
                await event.answer("⛔ Доступ к боту ограничен администратором.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Доступ ограничен", show_alert=True)
            elif isinstance(event, PreCheckoutQuery):
                await event.answer(ok=False, error_message="Доступ ограничен")
            return None
        return await handler(event, data)


class CommandResetMiddleware(BaseMiddleware):
    """Любая команда (кроме /cancel) сбрасывает незавершённый ввод — иначе следующий текст ушёл бы не туда."""

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        text = getattr(event, "text", None) or ""
        state = data.get("state")
        if text.startswith("/") and not text.startswith("/cancel") and state is not None:
            await state.clear()
        return await handler(event, data)
