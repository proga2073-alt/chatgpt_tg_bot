"""Админ-панель: статистика, пользователи, рассылка, модели и цены, промокоды, платежи."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.states import Form
from bot.utils import btn, esc, fmt_dt, kb, nav_row, pages, render
from config import settings
from db import repo
from db.session import SessionLocal
from llm.client import list_models
from services import billing

log = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.from_user.id.in_(settings.admin_set))
router.callback_query.filter(F.from_user.id.in_(settings.admin_set))

BACK = [btn("◀️ Админка", "a:menu")]
MODELS_PER_PAGE = 8
_tasks: set[asyncio.Task] = set()  # ссылки на фоновые задачи (иначе GC может их убить)


def _menu_kb():
    return kb(
        [btn("📊 Статистика", "a:stats"), btn("👤 Пользователи", "a:users")],
        [btn("🤖 Модели и цены", "a:mp:0"), btn("🎟 Промокоды", "a:promos")],
        [btn("💳 Платежи", "a:pay"), btn("📣 Рассылка", "a:bc")],
        [btn("◀️ Меню", "m:menu")],
    )


@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext) -> None:
    await state.clear()
    await render(m, "🛠 <b>Админ-панель</b>", _menu_kb())


@router.callback_query(F.data == "a:menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await render(cb, "🛠 <b>Админ-панель</b>", _menu_kb())


# ───────────── статистика ─────────────


@router.callback_query(F.data == "a:stats")
async def cb_stats(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        st = await repo.admin_stats(s)
    await render(
        cb,
        "📊 <b>Статистика</b>\n"
        f"👥 Пользователей: {st['users']} (новых за 24 ч: {st['new_day']}, заблокировано: {st['banned']})\n"
        f"🔥 Активных за 24 ч: {st['active_day']}\n"
        f"💬 Сообщений: {st['msgs_total']} (за 24 ч: {st['msgs_day']})\n"
        f"💳 Платежей: {st['pay_count']} на {st['pay_stars']} ⭐ (за 24 ч: {st['pay_stars_day']} ⭐)\n"
        f"💰 Остатки на балансах: {billing.fmt_stars(st['balances'])}",
        kb([btn("🔄 Обновить", "a:stats")], BACK),
    )


# ───────────── пользователи ─────────────


async def _user_card(event: Message | CallbackQuery, uid: int) -> None:
    async with SessionLocal() as s:
        user = await s.get(repo.User, uid)
        if user is None:
            await render(event, "Пользователь не найден.", kb(BACK))
            return
        convs, msgs = await repo.user_stats(s, uid)
    name = f"@{esc(user.username)}" if user.username else "без username"
    await render(
        event,
        f"👤 <b>{name}</b> · <code>{user.id}</code>\n"
        f"Баланс: {billing.fmt_stars(user.balance)}\n"
        f"Модель: <code>{esc(user.model or settings.openai_model)}</code>\n"
        f"Диалогов: {convs} · сообщений: {msgs}\n"
        f"Регистрация: {fmt_dt(user.created_at)}\n"
        f"Статус: {'🚫 заблокирован' if user.banned else '✅ активен'}",
        kb(
            [btn("➕ Начислить ⭐", f"a:ua:{uid}:add"), btn("➖ Списать ⭐", f"a:ua:{uid}:sub")],
            [btn("✅ Разблокировать" if user.banned else "🚫 Заблокировать", f"a:ub:{uid}")],
            BACK,
        ),
    )


@router.callback_query(F.data == "a:users")
async def cb_users(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.admin_find)
    await render(cb, "👤 Отправьте ID или @username пользователя.\n/cancel — отмена", kb(BACK))


@router.message(Form.admin_find, F.text)
async def on_find(m: Message, state: FSMContext) -> None:
    async with SessionLocal() as s:
        user = await repo.find_user(s, m.text or "")
    if user is None:
        await m.answer("Не найден (бот знает только тех, кто ему писал). Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    await _user_card(m, user.id)


@router.callback_query(F.data.startswith("a:u:"))
async def cb_user(cb: CallbackQuery) -> None:
    await _user_card(cb, int((cb.data or "").split(":")[2]))


@router.callback_query(F.data.startswith("a:ua:"))
async def cb_amount(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, uid, op = (cb.data or "").split(":")
    await state.set_state(Form.admin_amount)
    await state.update_data(target=int(uid), op=op)
    await render(
        cb,
        f"{'➕ Сколько ⭐ начислить' if op == 'add' else '➖ Сколько ⭐ списать'}? Например: 5 или 0.5\n/cancel — отмена",
        kb([btn("◀️ Назад", f"a:u:{uid}")]),
    )


@router.message(Form.admin_amount, F.text)
async def on_amount(m: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    try:
        value = float((m.text or "").replace(",", "."))
    except ValueError:
        value = 0
    if not 0 < value <= 100000:
        await m.answer("Нужно положительное число, например 5 или 0.5. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    uid, op = data["target"], data["op"]
    credits = round(value * settings.credits_per_star)
    async with SessionLocal() as s:
        await repo.adjust_balance(s, uid, credits if op == "add" else -credits)
        await s.commit()
    log.info("Admin %s %s %s credits for user %s", m.from_user.id if m.from_user else "?", op, credits, uid)
    if op == "add":
        with contextlib.suppress(Exception):
            await bot.send_message(uid, f"🎁 Администратор начислил вам {value:g} ⭐.")
    await _user_card(m, uid)


@router.callback_query(F.data.startswith("a:ub:"))
async def cb_ban(cb: CallbackQuery) -> None:
    uid = int((cb.data or "").split(":")[2])
    if uid in settings.admin_set:
        await cb.answer("Администратора заблокировать нельзя", show_alert=True)
        return
    async with SessionLocal() as s:
        user = await s.get(repo.User, uid)
        if user is not None:
            await repo.set_banned(s, uid, not user.banned)
            await s.commit()
    await _user_card(cb, uid)


# ───────────── рассылка ─────────────


@router.callback_query(F.data == "a:bc")
async def cb_broadcast(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.admin_broadcast)
    await render(cb, "📣 Отправьте сообщение для рассылки (текст, фото, документ — как есть, с форматированием).\n/cancel — отмена", kb(BACK))


@router.message(Form.admin_broadcast)
async def on_broadcast_msg(m: Message, state: FSMContext, bot: Bot) -> None:
    await state.set_state(Form.admin_bc_confirm)
    await state.update_data(from_chat=m.chat.id, msg_id=m.message_id)
    async with SessionLocal() as s:
        total = len(await repo.all_user_ids(s))
    await bot.copy_message(m.chat.id, m.chat.id, m.message_id)  # предпросмотр
    await m.answer(f"Отправить это сообщение {total} пользователям?", reply_markup=kb([btn("✅ Отправить", "a:bcgo"), btn("❌ Отмена", "a:menu")]))


async def _send_one(bot: Bot, uid: int, from_chat: int, msg_id: int) -> str:
    for _ in range(2):
        try:
            await bot.copy_message(uid, from_chat, msg_id)
            return "ok"
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramBadRequest:
            return "failed"
        except Exception:  # noqa: BLE001
            return "failed"
    return "failed"


async def _broadcast(bot: Bot, admin_id: int, from_chat: int, msg_id: int) -> None:
    async with SessionLocal() as s:
        ids = await repo.all_user_ids(s)
    result = {"ok": 0, "blocked": 0, "failed": 0}
    for uid in ids:
        result[await _send_one(bot, uid, from_chat, msg_id)] += 1
        await asyncio.sleep(0.05)  # ≈20 сообщений в секунду — в пределах лимитов Telegram
    with contextlib.suppress(Exception):
        await bot.send_message(admin_id, f"📣 Рассылка завершена: доставлено {result['ok']}, заблокировали бота {result['blocked']}, ошибок {result['failed']}.")


@router.callback_query(F.data == "a:bcgo")
async def cb_broadcast_go(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    if "msg_id" not in data:
        await cb.answer("Нет сообщения для рассылки", show_alert=True)
        return
    task = asyncio.create_task(_broadcast(bot, cb.from_user.id, data["from_chat"], data["msg_id"]))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    await render(cb, "📣 Рассылка запущена. Итог придёт отдельным сообщением.", kb(BACK))


# ───────────── модели и цены ─────────────


def _model_flags(name: str) -> str:
    return ("🙈 " if not billing.is_enabled(name) else "") + billing.price_label(name)


@router.callback_query(F.data.startswith("a:mp:"))
async def cb_models(cb: CallbackQuery) -> None:
    models = await list_models()
    total = pages(len(models), MODELS_PER_PAGE)
    page = min(max(int((cb.data or "a:mp:0").split(":")[2]), 0), total - 1)
    rows = [
        [btn(f"{models[i]} · {_model_flags(models[i])}", f"a:m:{i}")]
        for i in range(page * MODELS_PER_PAGE, min(len(models), (page + 1) * MODELS_PER_PAGE))
    ]
    if total > 1:
        rows.append(nav_row("a:mp:", page, total))
    rows.append(BACK)
    await render(cb, "🤖 <b>Модели и цены</b>\nНажмите на модель, чтобы изменить цену или скрыть её. 🙈 — скрыта от пользователей.", kb(*rows))


async def _model_by_idx(idx: int) -> str | None:
    models = await list_models()
    return models[idx] if 0 <= idx < len(models) else None


async def _model_card(event: Message | CallbackQuery, idx: int, name: str) -> None:
    ov = billing.override(name) or {}
    manual = " (задан вручную)" if ov.get("multiplier") is not None else ""
    free_manual = " (вручную)" if ov.get("free") is not None else ""
    await render(
        event,
        f"🤖 <b>{esc(name)}</b>\n"
        f"Показ: {'скрыта 🙈' if not billing.is_enabled(name) else 'доступна'}\n"
        f"Тип: {'бесплатная' if billing.is_free(name) else 'платная'}{free_manual}\n"
        f"Множитель: {billing.multiplier(name):g}{manual}\n"
        f"Цена сообщения: {billing.price_label(name)}",
        kb(
            [btn("💰 Сделать платной" if billing.is_free(name) else "🆓 Сделать бесплатной", f"a:mf:{idx}")],
            [btn("✖️ Задать множитель", f"a:mx:{idx}"), btn("👁 Показать" if not billing.is_enabled(name) else "🙈 Скрыть", f"a:mh:{idx}")],
            [btn("♻️ Сбросить к .env", f"a:mr:{idx}")],
            [btn("◀️ К списку", "a:mp:0")],
        ),
    )


async def _apply(name: str, **fields) -> None:
    async with SessionLocal() as s:
        await repo.upsert_model_config(s, name, **fields)
        await s.commit()
    await billing.reload()


@router.callback_query(F.data.regexp(r"^a:m:\d+$"))
async def cb_model(cb: CallbackQuery) -> None:
    idx = int((cb.data or "").split(":")[2])
    if (name := await _model_by_idx(idx)) is None:
        await cb.answer("Список обновился", show_alert=True)
        return
    await _model_card(cb, idx, name)


@router.callback_query(F.data.regexp(r"^a:m[fhr]:\d+$"))
async def cb_model_action(cb: CallbackQuery) -> None:
    _, action, idx_s = (cb.data or "").split(":")
    idx = int(idx_s)
    if (name := await _model_by_idx(idx)) is None:
        await cb.answer("Список обновился", show_alert=True)
        return
    if action == "mf":
        await _apply(name, free=not billing.is_free(name))
    elif action == "mh":
        await _apply(name, enabled=not billing.is_enabled(name))
    else:  # mr
        async with SessionLocal() as s:
            await repo.delete_model_config(s, name)
            await s.commit()
        await billing.reload()
    await _model_card(cb, idx, name)


@router.callback_query(F.data.regexp(r"^a:mx:\d+$"))
async def cb_model_mult(cb: CallbackQuery, state: FSMContext) -> None:
    idx = int((cb.data or "").split(":")[2])
    if (name := await _model_by_idx(idx)) is None:
        await cb.answer("Список обновился", show_alert=True)
        return
    await state.set_state(Form.admin_mult)
    await state.update_data(model=name, idx=idx)
    await render(
        cb,
        f"✖️ Новый множитель для <code>{esc(name)}</code> (сейчас {billing.multiplier(name):g}).\n"
        f"Цена сообщения = множитель × {settings.message_base_credits / settings.credits_per_star:g} ⭐. Например: 0.8 или 2\n/cancel — отмена",
        kb([btn("◀️ Назад", f"a:m:{idx}")]),
    )


@router.message(Form.admin_mult, F.text)
async def on_model_mult(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        value = float((m.text or "").replace(",", "."))
    except ValueError:
        value = 0
    if not 0 < value <= 100:
        await m.answer("Нужно число от 0 до 100, например 0.8. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    await _apply(data["model"], multiplier=value)
    await _model_card(m, data["idx"], data["model"])


# ───────────── промокоды ─────────────


async def _promos(event: Message | CallbackQuery) -> None:
    async with SessionLocal() as s:
        promos = await repo.list_promos(s)
    lines = [
        f"<code>{esc(p.code)}</code> — {billing.fmt_stars(p.credits)}, использовано {p.used}/{p.max_uses or '∞'}"
        for p in promos
    ] or ["Промокодов пока нет."]
    rows = [[btn(f"🗑 {p.code}", f"a:pd:{p.code}")] for p in promos[:10]]
    await render(
        event,
        "🎟 <b>Промокоды</b>\n" + "\n".join(lines),
        kb(*rows, [btn("➕ Создать", "a:pn")], BACK),
    )


@router.callback_query(F.data == "a:promos")
async def cb_promos(cb: CallbackQuery) -> None:
    await _promos(cb)


@router.callback_query(F.data == "a:pn")
async def cb_promo_new(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.admin_promo_new)
    await render(
        cb,
        "➕ Формат: <code>КОД ЗВЁЗДЫ [ЛИМИТ]</code>\nНапример: <code>WELCOME 5 100</code> — 5 ⭐, 100 активаций (без лимита — не указывайте).\n/cancel — отмена",
        kb([btn("◀️ Назад", "a:promos")]),
    )


@router.message(Form.admin_promo_new, F.text)
async def on_promo_new(m: Message, state: FSMContext) -> None:
    parts = (m.text or "").split()
    try:
        code = parts[0].upper()
        stars = float(parts[1].replace(",", "."))
        limit = int(parts[2]) if len(parts) > 2 else 0
        ok = re.fullmatch(r"[A-Z0-9_-]{3,32}", code) is not None and 0 < stars <= 100000 and limit >= 0
    except (IndexError, ValueError):
        ok = False
    if not ok:
        await m.answer("Не понял формат. Пример: WELCOME 5 100 (код из латиницы/цифр, 3–32 символа). Попробуйте ещё раз или /cancel.")
        return
    async with SessionLocal() as s:
        created = await repo.create_promo(s, code, round(stars * settings.credits_per_star), limit)
        await s.commit()
    if not created:
        await m.answer("Такой код уже существует. Придумайте другой или /cancel.")
        return
    await state.clear()
    await _promos(m)


@router.callback_query(F.data.startswith("a:pd:"))
async def cb_promo_delete(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        await repo.delete_promo(s, (cb.data or "").split(":", 2)[2])
        await s.commit()
    await _promos(cb)


# ───────────── платежи и возвраты ─────────────


def _short(charge_id: str) -> str:
    return hashlib.sha1(charge_id.encode()).hexdigest()[:12]


@router.callback_query(F.data == "a:pay")
async def cb_payments(cb: CallbackQuery) -> None:
    async with SessionLocal() as s:
        pays = await repo.recent_payments(s, 10)
    lines = [
        f"{i}. {p.created_at:%d.%m %H:%M} · <code>{p.user_id}</code> · {p.stars} ⭐ {'↩️ возвращён' if p.refunded else '✅'}"
        for i, p in enumerate(pays, 1)
    ] or ["Платежей пока нет."]
    rows = [[btn(f"↩️ Вернуть №{i}", f"a:rf:{_short(p.charge_id)}")] for i, p in enumerate(pays, 1) if not p.refunded]
    await render(cb, "💳 <b>Последние платежи</b>\n" + "\n".join(lines), kb(*rows[:8], BACK))


async def _find_payment(short: str):
    async with SessionLocal() as s:
        for p in await repo.recent_payments(s, 50):
            if _short(p.charge_id) == short:
                return p
    return None


@router.callback_query(F.data.startswith("a:rf:"))
async def cb_refund_ask(cb: CallbackQuery) -> None:
    p = await _find_payment((cb.data or "").split(":")[2])
    if p is None or p.refunded:
        await cb.answer("Платёж не найден или уже возвращён", show_alert=True)
        return
    await render(
        cb,
        f"↩️ Вернуть <b>{p.stars} ⭐</b> пользователю <code>{p.user_id}</code>?\nЗачисленные за этот платёж кредиты будут списаны с его баланса (не ниже нуля).",
        kb([btn("Да, вернуть", f"a:rfok:{_short(p.charge_id)}"), btn("Отмена", "a:pay")]),
    )


@router.callback_query(F.data.startswith("a:rfok:"))
async def cb_refund(cb: CallbackQuery, bot: Bot) -> None:
    p = await _find_payment((cb.data or "").split(":")[2])
    if p is None or p.refunded:
        await cb.answer("Платёж не найден или уже возвращён", show_alert=True)
        return
    try:
        await bot.refund_star_payment(user_id=p.user_id, telegram_payment_charge_id=p.charge_id)
    except Exception as exc:  # noqa: BLE001
        log.error("Refund failed for %s: %s", p.charge_id, exc)
        await cb.answer(f"Telegram отклонил возврат: {str(exc)[:150]}", show_alert=True)
        return
    async with SessionLocal() as s:
        await repo.mark_refunded(s, p.charge_id)
        await s.commit()
    with contextlib.suppress(Exception):
        await bot.send_message(p.user_id, f"↩️ Вам возвращено {p.stars} ⭐ за пополнение баланса.")
    await cb_payments(cb)
