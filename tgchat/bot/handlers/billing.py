"""Баланс, пополнение Telegram Stars, промокоды."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from bot.keyboards import buy_kb
from bot.states import Form
from bot.utils import btn, esc, kb, load_user, render
from config import settings
from db import repo
from db.session import SessionLocal
from services import billing

log = logging.getLogger(__name__)
router = Router(name="billing")

PROMO_REPLIES = {
    "not_found": "Такого промокода нет.",
    "used": "Вы уже использовали этот промокод.",
    "exhausted": "Лимит активаций этого промокода исчерпан.",
}


async def show_balance(event: Message | CallbackQuery) -> None:
    user = await load_user(event.from_user.id)  # type: ignore[union-attr]
    model = user.model or settings.openai_model
    cost = billing.cost_credits(model)
    price = "бесплатна" if cost == 0 else f"{billing.fmt_stars(cost)} за сообщение"
    image_price = billing.fmt_stars(settings.image_cost_credits)
    await render(
        event,
        f"⭐ <b>Баланс: {billing.fmt_stars(user.balance)}</b>\n"
        f"Модель <code>{esc(model)}</code>: {price}\n"
        f"Изображение: {image_price}\n"
        f"Бесплатные модели: {esc(', '.join(sorted(settings.free_model_set)) or 'нет')}\n\n"
        "Пополнение — Telegram Stars:",
        buy_kb(),
    )


@router.message(Command("balance"))
async def cmd_balance(m: Message) -> None:
    await show_balance(m)


@router.message(Command("buy"))
async def cmd_buy(m: Message) -> None:
    await show_balance(m)


@router.callback_query(F.data == "m:bal")
async def cb_balance(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_balance(cb)


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, bot: Bot) -> None:
    stars = int((cb.data or "buy:0").split(":")[1])
    if stars not in settings.star_pack_list:
        await cb.answer("Пакет недоступен. Откройте /buy заново.", show_alert=True)
        return
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title="Пополнение баланса",
        description=f"{stars} ⭐ на платные модели и изображения",
        payload=f"topup:{stars}",
        currency="XTR",  # Telegram Stars; provider_token не нужен
        prices=[LabeledPrice(label=f"{stars} ⭐", amount=stars)],
    )
    await cb.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    ok = q.currency == "XTR" and q.invoice_payload.startswith("topup:") and q.total_amount in settings.star_pack_list
    await q.answer(ok=ok, error_message=None if ok else "Пакет недоступен. Откройте /buy заново.")


@router.message(F.successful_payment)
async def on_paid(m: Message) -> None:
    assert m.from_user and m.successful_payment
    sp = m.successful_payment
    stars = sp.total_amount
    try:
        async with SessionLocal() as s:
            user = await repo.get_or_create_user(s, m.from_user.id, m.from_user.username)
            await repo.record_payment(s, sp.telegram_payment_charge_id, user.id, stars, stars * settings.credits_per_star)
            await s.commit()
            balance = await repo.get_balance(s, user.id)
    except Exception:  # noqa: BLE001
        log.critical("PAYMENT NOT CREDITED user=%s stars=%s charge_id=%s", m.from_user.id, stars, sp.telegram_payment_charge_id, exc_info=True)
        await m.answer("Оплата получена, но зачислить её не удалось. Напишите в поддержку: /paysupport")
        return
    log.info("Payment: user=%s +%s stars (charge %s)", m.from_user.id, stars, sp.telegram_payment_charge_id)
    await m.answer(f"Оплата получена: +{stars} ⭐. Баланс: {billing.fmt_stars(balance)}.", reply_markup=kb([btn("◀️ Меню", "m:menu")]))


@router.message(Command("paysupport"))
async def cmd_paysupport(m: Message) -> None:
    contact = settings.support_contact or "администратору бота"
    await m.answer(f"По вопросам оплаты и возвратов пишите {contact}. Укажите, что и когда оплачивали.")


# ───────────── промокоды ─────────────


async def _redeem(m: Message, code: str) -> None:
    assert m.from_user
    async with SessionLocal() as s:
        user = await repo.get_or_create_user(s, m.from_user.id, m.from_user.username)
        status, credits = await repo.redeem_promo(s, code, user.id)
        await s.commit()
        balance = await repo.get_balance(s, user.id)
    if status == "ok":
        await m.answer(f"🎉 Промокод активирован: +{billing.fmt_stars(credits)}. Баланс: {billing.fmt_stars(balance)}.")
    else:
        await m.answer(PROMO_REPLIES[status])


@router.message(Command("promo"))
async def cmd_promo(m: Message, command: CommandObject, state: FSMContext) -> None:
    if command.args and command.args.strip():
        await _redeem(m, command.args.strip())
        return
    await state.set_state(Form.promo)
    await m.answer("🎟 Отправьте промокод.\n/cancel — отмена")


@router.callback_query(F.data == "b:promo")
async def cb_promo(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.promo)
    await render(cb, "🎟 Отправьте промокод.\n/cancel — отмена", kb([btn("◀️ Назад", "m:bal")]))


@router.message(Form.promo, F.text)
async def on_promo(m: Message, state: FSMContext) -> None:
    await state.clear()
    await _redeem(m, (m.text or "").strip())
