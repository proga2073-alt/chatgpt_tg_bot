"""Старт, меню, справка, новый диалог, отмена ввода, личная статистика."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import tools
from bot.keyboards import main_menu
from bot.utils import btn, esc, fmt_dt, kb, load_user, render
from config import settings
from db import repo
from db.session import SessionLocal
from services import billing, chat

router = Router(name="common")


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_set


async def _menu_text(user_id: int) -> str:
    user = await load_user(user_id)
    model = user.model or settings.openai_model
    free = ", ".join(sorted(settings.free_model_set)) or "—"
    return (
        "<b>Главное меню</b>\n"
        f"🤖 Модель: <code>{esc(model)}</code> — {billing.price_label(model)}\n"
        f"⭐ Баланс: {billing.fmt_stars(user.balance)}\n"
        f"🆓 Бесплатные модели: {esc(free)}\n\n"
        "Просто напишите сообщение — я отвечу. Можно присылать файлы и просить нарисовать картинку."
    )


@router.message(CommandStart())
async def cmd_start(m: Message) -> None:
    assert m.from_user
    await m.answer(
        "👋 Привет! Я ИИ-ассистент: отвечаю на вопросы, пишу и правлю тексты и код, "
        "рисую изображения и работаю с файлами.\n\n" + await _menu_text(m.from_user.id),
        parse_mode="HTML",
        reply_markup=main_menu(_is_admin(m.from_user.id)),
    )


@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext) -> None:
    assert m.from_user
    await state.clear()
    await render(m, await _menu_text(m.from_user.id), main_menu(_is_admin(m.from_user.id)))


@router.callback_query(F.data == "m:menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await render(cb, await _menu_text(cb.from_user.id), main_menu(_is_admin(cb.from_user.id)))


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Отменено.", reply_markup=kb([btn("◀️ Меню", "m:menu")]))


# ───────────── справка ─────────────


def _help_text(user_id: int) -> str:
    lines = [
        "<b>Что я умею</b>",
        "💬 Диалоги с памятью контекста: /chats — список, поиск, переименование, удаление, экспорт",
        "🤖 Выбор модели и параметров: /model, /settings, роли — /persona",
        "🎨 Рисую и редактирую изображения (платно, ⭐) — просто попросите",
        "📎 Присылайте файлы с подписью-задачей — я прочитаю, изменю и верну файл. /files — ваши файлы",
        "🔄 Под ответом есть «Заново», пока идёт ответ — «Стоп»",
        "⭐ Баланс: /balance, /buy, /promo — промокод",
        "📈 /stats — ваша статистика, /export — выгрузить диалог файлом",
    ]
    if tools.shell_available(_is_admin(user_id)):
        lines.append("🖥 Для вас включено выполнение команд cmd — только после вашего подтверждения кнопкой")
    if _is_admin(user_id):
        lines.append("🛠 /admin — админ-панель")
    lines.append("\nПлатные модели списывают ⭐ за каждое сообщение; цены видны в /model.")
    return "\n".join(lines)


@router.message(Command("help"))
async def cmd_help(m: Message) -> None:
    assert m.from_user
    await render(m, _help_text(m.from_user.id), kb([btn("◀️ Меню", "m:menu")]))


@router.callback_query(F.data == "m:help")
async def cb_help(cb: CallbackQuery) -> None:
    await render(cb, _help_text(cb.from_user.id), kb([btn("◀️ Меню", "m:menu")]))


# ───────────── новый диалог ─────────────


async def _new(event: Message | CallbackQuery) -> None:
    user = event.from_user
    assert user
    await chat.new_conversation(user.id, user.username)
    await render(
        event,
        "🆕 Начат новый диалог — контекст очищен. Пишите сообщение.",
        kb([btn("💬 Диалоги", "m:chats"), btn("◀️ Меню", "m:menu")]),
    )


@router.message(Command("new"))
async def cmd_new(m: Message) -> None:
    await _new(m)


@router.callback_query(F.data.in_({"m:new", "newchat"}))
async def cb_new(cb: CallbackQuery) -> None:
    await _new(cb)


# ───────────── статистика ─────────────


@router.message(Command("stats"))
async def cmd_stats(m: Message) -> None:
    assert m.from_user
    user = await load_user(m.from_user.id)
    async with SessionLocal() as s:
        convs, msgs = await repo.user_stats(s, user.id)
    await m.answer(
        "<b>📈 Ваша статистика</b>\n"
        f"Диалогов: {convs}\nОтправлено сообщений: {msgs}\n"
        f"Баланс: {billing.fmt_stars(user.balance)}\n"
        f"С нами с {fmt_dt(user.created_at)}",
        parse_mode="HTML",
    )
