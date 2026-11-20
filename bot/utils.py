"""Общие помощники для хэндлеров: клавиатуры, правка сообщений, пагинация."""
from __future__ import annotations

import asyncio
import contextlib
import html
import math
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from db.models import User
from db.session import SessionLocal

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
EMPTY_KB = InlineKeyboardMarkup(inline_keyboard=[])  # передать в edit_text, чтобы убрать кнопки
esc = html.escape


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def grid(buttons: list[InlineKeyboardButton], width: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + width] for i in range(0, len(buttons), width)]


def pages(total: int, per: int) -> int:
    return max(1, math.ceil(total / per))


def nav_row(prefix: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}{page - 1}"))
    row.append(btn(f"{page + 1}/{total_pages}", "noop"))
    if page < total_pages - 1:
        row.append(btn("▶️", f"{prefix}{page + 1}"))
    return row


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M") + " UTC"


async def load_user(user_id: int) -> User:
    async with SessionLocal() as s:
        user = await s.get(User, user_id)
    assert user is not None  # GuardMiddleware создаёт пользователя до хэндлера
    return user


async def edit(msg: Message, html_text: str, plain: str | None = None, markup: InlineKeyboardMarkup | None = None) -> None:
    """Правит сообщение HTML-ом; при ошибке разметки откатывается на plain-текст."""
    try:
        await msg.edit_text(html_text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        if plain is None:
            raise
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text(plain, parse_mode=None, link_preview_options=NO_PREVIEW, reply_markup=markup)


async def send(bot: Bot, chat_id: int, html_text: str, plain: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    try:
        return await bot.send_message(
            chat_id, html_text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup
        )
    except TelegramBadRequest:
        return await bot.send_message(chat_id, plain, parse_mode=None, link_preview_options=NO_PREVIEW, reply_markup=markup)


async def render(event: CallbackQuery | Message, text: str, markup: InlineKeyboardMarkup | None = None, toast: str | None = None) -> None:
    """Показывает экран меню: правит сообщение с кнопкой (callback) или отправляет новое. Отвечает на callback."""
    if isinstance(event, CallbackQuery):
        msg = event.message
        if isinstance(msg, Message):
            try:
                await msg.edit_text(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
            except TelegramBadRequest as exc:
                if "not modified" not in str(exc).lower():  # например, у сообщения нет текста (документ)
                    await msg.answer(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
        await event.answer(toast)
    else:
        await event.answer(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)


async def with_flood_retry(func: Callable[..., Awaitable[Any]], *args: Any) -> None:
    """Для финальных правок: ждём retry_after и пробуем ещё раз."""
    for _ in range(3):
        try:
            await func(*args)
            return
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
