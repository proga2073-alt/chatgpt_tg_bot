"""Диалоги: список, поиск, переименование, удаление, экспорт."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot.states import Form
from bot.utils import btn, esc, kb, load_user, nav_row, pages, render
from db import repo
from db.session import SessionLocal

router = Router(name="chats")
PER_PAGE = 6
PER_SEARCH = 10


async def show_chats(event: Message | CallbackQuery, page: int = 0, query: str | None = None) -> None:
    user_id = event.from_user.id  # type: ignore[union-attr]
    async with SessionLocal() as s:
        convs = await repo.list_conversations(s, user_id, query)
    current = (await load_user(user_id)).current_conversation_id
    per = PER_SEARCH if query else PER_PAGE
    total = pages(len(convs), per)
    page = min(max(page, 0), total - 1)
    rows = [
        [btn(("▶️ " if c.id == current else "") + c.title[:40], f"c:open:{c.id}")]
        for c in convs[page * per : (page + 1) * per]
    ]
    if total > 1 and not query:
        rows.append(nav_row("c:pg:", page, total))
    rows.append([btn("➕ Новый", "m:new"), btn("🔍 Поиск", "c:search")])
    rows.append([btn("◀️ Меню", "m:menu")])
    if query:
        head = f"🔍 Поиск «{esc(query)}»: найдено {len(convs)}" + (f", показаны первые {PER_SEARCH}" if len(convs) > PER_SEARCH else "")
    else:
        head = f"💬 <b>Диалоги</b> ({len(convs)})" if convs else "💬 <b>Диалогов пока нет.</b> Напишите сообщение, чтобы начать."
    await render(event, head, kb(*rows))


@router.message(Command("chats"))
async def cmd_chats(m: Message, command: CommandObject) -> None:
    await show_chats(m, 0, (command.args or "").strip() or None)


@router.message(Command("search"))
async def cmd_search(m: Message, command: CommandObject) -> None:
    q = (command.args or "").strip()
    await show_chats(m, 0, q or None)


@router.callback_query(F.data == "m:chats")
async def cb_chats(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_chats(cb, 0)


@router.callback_query(F.data.startswith("c:pg:"))
async def cb_page(cb: CallbackQuery) -> None:
    await show_chats(cb, int((cb.data or "c:pg:0").split(":")[2]))


@router.callback_query(F.data == "c:search")
async def cb_search(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.search)
    await render(cb, "🔍 Что искать? Отправьте слово из названия или текста диалога.\n/cancel — отмена", kb([btn("◀️ Назад", "m:chats")]))


@router.message(Form.search, F.text)
async def on_search(m: Message, state: FSMContext) -> None:
    await state.clear()
    await show_chats(m, 0, (m.text or "").strip()[:100])


@router.callback_query(F.data.startswith("c:open:"))
async def cb_open(cb: CallbackQuery) -> None:
    conv_id = int((cb.data or "").split(":")[2])
    async with SessionLocal() as s:
        user = await repo.get_or_create_user(s, cb.from_user.id, cb.from_user.username)
        conv = await repo.get_conversation(s, user.id, conv_id)
        if conv is None:
            await cb.answer("Диалог не найден", show_alert=True)
            return
        user.current_conversation_id = conv.id
        last = await repo.recent_messages(s, conv.id, 2)
        await s.commit()
    preview = "\n\n".join(
        f"{'🧑' if m.role == 'user' else '🤖'} {esc(m.content[:300])}{'…' if len(m.content) > 300 else ''}" for m in last
    ) or "<i>Пока пусто.</i>"
    await render(
        cb,
        f"<b>{esc(conv.title)}</b>\n<i>Диалог выбран — пишите сообщение, и разговор продолжится.</i>\n\n{preview}",
        kb(
            [btn("✏️ Переименовать", f"c:ren:{conv.id}"), btn("📤 Экспорт", f"c:exp:{conv.id}")],
            [btn("🗑 Удалить", f"c:del:{conv.id}")],
            [btn("◀️ К списку", "c:pg:0")],
        ),
    )


@router.callback_query(F.data.startswith("c:ren:"))
async def cb_rename(cb: CallbackQuery, state: FSMContext) -> None:
    conv_id = int((cb.data or "").split(":")[2])
    await state.set_state(Form.rename)
    await state.update_data(conv_id=conv_id)
    await render(cb, "✏️ Отправьте новое название диалога.\n/cancel — отмена", kb([btn("◀️ Назад", f"c:open:{conv_id}")]))


@router.message(Form.rename, F.text)
async def on_rename(m: Message, state: FSMContext) -> None:
    assert m.from_user
    conv_id = (await state.get_data()).get("conv_id")
    await state.clear()
    title = " ".join((m.text or "").split())[:100]
    if not title or conv_id is None:
        await m.answer("Название не может быть пустым.")
        return
    async with SessionLocal() as s:
        conv = await repo.get_conversation(s, m.from_user.id, conv_id)
        if conv is None:
            await m.answer("Диалог не найден.")
            return
        conv.title = title
        await s.commit()
    await m.answer("✅ Переименовано.", reply_markup=kb([btn("💬 К диалогам", "m:chats")]))


@router.callback_query(F.data.startswith("c:del:"))
async def cb_delete(cb: CallbackQuery) -> None:
    conv_id = int((cb.data or "").split(":")[2])
    await render(
        cb,
        "🗑 Удалить этот диалог вместе с историей? Действие необратимо.",
        kb([btn("Да, удалить", f"c:delok:{conv_id}"), btn("Отмена", f"c:open:{conv_id}")]),
    )


@router.callback_query(F.data.startswith("c:delok:"))
async def cb_delete_ok(cb: CallbackQuery) -> None:
    conv_id = int((cb.data or "").split(":")[2])
    async with SessionLocal() as s:
        user = await repo.get_or_create_user(s, cb.from_user.id, cb.from_user.username)
        conv = await repo.get_conversation(s, user.id, conv_id)
        if conv is not None:
            await repo.delete_conversation(s, user, conv)
            await s.commit()
    await show_chats(cb, 0)


# ───────────── экспорт ─────────────


async def send_export(bot: Bot, chat_id: int, user_id: int, conv_id: int | None) -> bool:
    async with SessionLocal() as s:
        if conv_id is None:
            conv_id = (await repo.get_or_create_user(s, user_id, None)).current_conversation_id
        conv = await repo.get_conversation(s, user_id, conv_id) if conv_id else None
        msgs = await repo.get_messages(s, conv.id) if conv else []
    if conv is None or not msgs:
        return False
    lines = [f"Диалог: {conv.title}", f"Создан: {conv.created_at:%d.%m.%Y %H:%M} UTC", "=" * 40, ""]
    for m in msgs:
        lines += [("Вы" if m.role == "user" else "Ассистент") + f" · {m.created_at:%d.%m.%Y %H:%M}", m.content, ""]
    await bot.send_document(
        chat_id,
        BufferedInputFile("\n".join(lines).encode("utf-8"), filename=f"dialog_{conv.id}.txt"),
        caption=f"📤 {conv.title[:100]}",
    )
    return True


@router.message(Command("export"))
async def cmd_export(m: Message, bot: Bot) -> None:
    assert m.from_user
    if not await send_export(bot, m.chat.id, m.from_user.id, None):
        await m.answer("В текущем диалоге пока нет сообщений.")


@router.callback_query(F.data.startswith("c:exp:"))
async def cb_export(cb: CallbackQuery, bot: Bot) -> None:
    conv_id = int((cb.data or "").split(":")[2])
    ok = await send_export(bot, cb.from_user.id, cb.from_user.id, conv_id)
    await cb.answer("Файл отправлен" if ok else "Диалог пуст", show_alert=not ok)
