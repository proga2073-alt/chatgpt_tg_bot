"""Файлы и вложения, подтверждение команд cmd."""
from __future__ import annotations

import io
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, Message

from bot import tools
from bot.handlers.chat import run_turn
from bot.utils import btn, esc, kb, render
from config import settings
from services import workspace

log = logging.getLogger(__name__)
router = Router(name="agent")


# ───────────── загрузка файлов и фото ─────────────


@router.message(StateFilter(None), F.document | F.photo)
async def on_upload(m: Message, bot: Bot) -> None:
    assert m.from_user
    if m.document:
        tg_file, name, size = m.document, m.document.file_name or f"file_{m.message_id}", m.document.file_size or 0
    else:
        tg_file = m.photo[-1]  # type: ignore[index]
        name, size = f"photo_{m.message_id}.jpg", tg_file.file_size or 0
    limit = settings.max_upload_mb * 1024 * 1024
    if size > limit:
        await m.answer(f"Файл слишком большой: максимум {settings.max_upload_mb} МБ.")
        return
    buf = io.BytesIO()
    await bot.download(tg_file, destination=buf)
    if buf.tell() > limit:
        await m.answer(f"Файл слишком большой: максимум {settings.max_upload_mb} МБ.")
        return
    path = workspace.save(m.from_user.id, name, buf.getvalue())
    workspace.note_upload(m.from_user.id, path.name)
    caption = (m.caption or "").strip()
    if not caption:
        await m.answer(f"📎 Файл «{path.name}» сохранён. Напишите, что с ним сделать (например: «исправь ошибки» или «сделай фон синим»).")
        return
    await run_turn(bot, m.chat.id, m.from_user.id, m.from_user.username, caption)


# ───────────── список файлов ─────────────


async def _files_screen(event: Message | CallbackQuery) -> None:
    files = workspace.list_files(event.from_user.id)  # type: ignore[union-attr]
    lines = [f"• <code>{esc(n)}</code> — {size / 1024:.1f} КБ" for n, size in files[:30]] or ["Пока пусто."]
    await render(
        event,
        "📂 <b>Ваши файлы</b>\nСюда попадают присланные вами файлы и всё, что создал ассистент.\n\n" + "\n".join(lines),
        kb([btn("🗑 Очистить", "f:clear")], [btn("◀️ Меню", "m:menu")]),
    )


@router.message(Command("files"))
async def cmd_files(m: Message) -> None:
    await _files_screen(m)


@router.callback_query(F.data == "m:files")
async def cb_files(cb: CallbackQuery) -> None:
    await _files_screen(cb)


@router.callback_query(F.data == "f:clear")
async def cb_files_clear(cb: CallbackQuery) -> None:
    workspace.clear(cb.from_user.id)
    await _files_screen(cb)


# ───────────── подтверждение команд cmd ─────────────


@router.callback_query(F.data.startswith("sh:"))
async def cb_shell(cb: CallbackQuery) -> None:
    _, verdict, token = (cb.data or "").split(":", 2)
    found = tools.resolve_shell(token, cb.from_user.id, verdict == "ok")
    if not found:
        await cb.answer("Запрос устарел", show_alert=True)
        return
    await cb.answer("Выполняю…" if verdict == "ok" else "Отклонено")
