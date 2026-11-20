"""Запросы к БД. Все функции принимают открытую сессию; commit — на вызывающем."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, case, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import (
    DEFAULT_TITLE,
    Conversation,
    Message,
    ModelConfig,
    Payment,
    PromoCode,
    PromoUse,
    Usage,
    User,
    utcnow,
)

# ───────────── пользователи ─────────────


async def get_or_create_user(s: AsyncSession, user_id: int, username: str | None) -> User:
    user = await s.get(User, user_id)
    if user is None:
        user = User(id=user_id, username=username, balance=settings.welcome_credits)
        s.add(user)
        await s.flush()
    elif username != user.username:
        user.username = username
    return user


async def find_user(s: AsyncSession, query: str) -> User | None:
    q = query.strip().lstrip("@")
    if q.isdigit():
        return await s.get(User, int(q))
    return await s.scalar(select(User).where(func.lower(User.username) == q.lower()))


async def set_banned(s: AsyncSession, user_id: int, banned: bool) -> None:
    await s.execute(update(User).where(User.id == user_id).values(banned=banned))


async def all_user_ids(s: AsyncSession) -> list[int]:
    return list((await s.scalars(select(User.id).where(User.banned.is_(False)))).all())


async def user_stats(s: AsyncSession, user_id: int) -> tuple[int, int]:
    """→ (диалогов, сообщений пользователя)."""
    convs = await s.scalar(select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id))
    msgs = await s.scalar(
        select(func.count())
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == user_id, Message.role == "user")
    )
    return convs or 0, msgs or 0


# ───────────── диалоги и сообщения ─────────────


async def create_conversation(s: AsyncSession, user_id: int, title: str = DEFAULT_TITLE) -> Conversation:
    conv = Conversation(user_id=user_id, title=title[:200])
    s.add(conv)
    await s.flush()
    return conv


async def get_conversation(s: AsyncSession, user_id: int, conv_id: int) -> Conversation | None:
    conv = await s.get(Conversation, conv_id)
    return conv if conv is not None and conv.user_id == user_id else None


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def list_conversations(
    s: AsyncSession, user_id: int, q: str | None = None, limit: int = 200
) -> list[Conversation]:
    stmt = select(Conversation).where(Conversation.user_id == user_id)
    if q:
        like = _like(q)
        in_messages = exists().where(
            and_(Message.conversation_id == Conversation.id, Message.content.ilike(like, escape="\\"))
        )
        stmt = stmt.where(or_(Conversation.title.ilike(like, escape="\\"), in_messages))
    stmt = stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit)
    return list((await s.scalars(stmt)).all())


async def delete_conversation(s: AsyncSession, user: User, conv: Conversation) -> None:
    await s.execute(delete(Message).where(Message.conversation_id == conv.id))
    await s.delete(conv)
    if user.current_conversation_id == conv.id:
        user.current_conversation_id = None


async def get_messages(s: AsyncSession, conv_id: int) -> list[Message]:
    stmt = select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)
    return list((await s.scalars(stmt)).all())


async def recent_messages(s: AsyncSession, conv_id: int, limit: int) -> list[Message]:
    stmt = select(Message).where(Message.conversation_id == conv_id).order_by(Message.id.desc()).limit(limit)
    return list(reversed((await s.scalars(stmt)).all()))


async def add_message(s: AsyncSession, conv_id: int, role: str, content: str) -> Message:
    msg = Message(conversation_id=conv_id, role=role, content=content)
    s.add(msg)
    await s.execute(update(Conversation).where(Conversation.id == conv_id).values(updated_at=utcnow()))
    await s.flush()
    return msg


async def delete_last_assistant(s: AsyncSession, conv_id: int) -> bool:
    """Удаляет последний ответ ассистента (для «Заново»). False — последним был не ассистент."""
    last = await s.scalar(select(Message).where(Message.conversation_id == conv_id).order_by(Message.id.desc()).limit(1))
    if last is not None and last.role == "assistant":
        await s.delete(last)
        await s.flush()
        return True
    return False


async def bump_usage(s: AsyncSession, user_id: int, limit: int) -> bool:
    """Увеличивает дневной счётчик. False — лимит исчерпан."""
    day = utcnow().strftime("%Y-%m-%d")
    row = await s.get(Usage, (user_id, day))
    if row is None:
        row = Usage(user_id=user_id, day=day, count=0)
        s.add(row)
    if limit and row.count >= limit:
        return False
    row.count += 1
    return True


# ───────────── баланс (кредиты) ─────────────


async def get_balance(s: AsyncSession, user_id: int) -> int:
    return int(await s.scalar(select(User.balance).where(User.id == user_id)) or 0)


async def charge(s: AsyncSession, user_id: int, cost: int) -> bool:
    """Атомарно списывает кредиты. False — не хватает средств."""
    res = await s.execute(
        update(User).where(User.id == user_id, User.balance >= cost).values(balance=User.balance - cost)
    )
    return res.rowcount == 1


async def add_credits(s: AsyncSession, user_id: int, amount: int) -> None:
    await s.execute(update(User).where(User.id == user_id).values(balance=User.balance + amount))


async def adjust_balance(s: AsyncSession, user_id: int, delta: int) -> None:
    """Ручная корректировка админом; баланс не уходит ниже нуля."""
    if delta >= 0:
        await add_credits(s, user_id, delta)
        return
    await s.execute(
        update(User)
        .where(User.id == user_id)
        .values(balance=case((User.balance > -delta, User.balance + delta), else_=0))
    )


async def record_payment(s: AsyncSession, charge_id: str, user_id: int, stars: int, credits: int) -> bool:
    """Фиксирует оплату и зачисляет кредиты. False — этот платёж уже был учтён."""
    try:
        s.add(Payment(charge_id=charge_id, user_id=user_id, stars=stars, credits=credits))
        await s.flush()
    except IntegrityError:
        await s.rollback()
        return False
    await add_credits(s, user_id, credits)
    return True


async def recent_payments(s: AsyncSession, limit: int = 10) -> list[Payment]:
    return list((await s.scalars(select(Payment).order_by(Payment.created_at.desc()).limit(limit))).all())


async def mark_refunded(s: AsyncSession, charge_id: str) -> Payment | None:
    """Помечает платёж возвращённым и списывает зачисленные за него кредиты (не ниже нуля)."""
    p = await s.get(Payment, charge_id)
    if p is None or p.refunded:
        return None
    p.refunded = True
    await adjust_balance(s, p.user_id, -p.credits)
    return p


# ───────────── админ: статистика ─────────────


async def admin_stats(s: AsyncSession) -> dict[str, int]:
    day = utcnow() - timedelta(days=1)

    async def count(stmt) -> int:
        return int(await s.scalar(stmt) or 0)

    return {
        "users": await count(select(func.count()).select_from(User)),
        "new_day": await count(select(func.count()).select_from(User).where(User.created_at >= day)),
        "banned": await count(select(func.count()).select_from(User).where(User.banned.is_(True))),
        "active_day": await count(
            select(func.count(func.distinct(Conversation.user_id)))
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.created_at >= day, Message.role == "user")
        ),
        "msgs_total": await count(select(func.count()).select_from(Message).where(Message.role == "user")),
        "msgs_day": await count(
            select(func.count()).select_from(Message).where(Message.role == "user", Message.created_at >= day)
        ),
        "pay_count": await count(select(func.count()).select_from(Payment).where(Payment.refunded.is_(False))),
        "pay_stars": await count(select(func.sum(Payment.stars)).where(Payment.refunded.is_(False))),
        "pay_stars_day": await count(
            select(func.sum(Payment.stars)).where(Payment.refunded.is_(False), Payment.created_at >= day)
        ),
        "balances": await count(select(func.sum(User.balance))),
    }


# ───────────── админ: модели ─────────────


async def list_model_configs(s: AsyncSession) -> list[ModelConfig]:
    return list((await s.scalars(select(ModelConfig))).all())


async def upsert_model_config(s: AsyncSession, model: str, **fields) -> None:
    row = await s.get(ModelConfig, model)
    if row is None:
        row = ModelConfig(model=model, enabled=True)
        s.add(row)
    for key, value in fields.items():
        setattr(row, key, value)


async def delete_model_config(s: AsyncSession, model: str) -> None:
    await s.execute(delete(ModelConfig).where(ModelConfig.model == model))


# ───────────── промокоды ─────────────


async def create_promo(s: AsyncSession, code: str, credits: int, max_uses: int) -> bool:
    try:
        s.add(PromoCode(code=code, credits=credits, max_uses=max_uses, used=0))
        await s.flush()
        return True
    except IntegrityError:
        await s.rollback()
        return False


async def list_promos(s: AsyncSession) -> list[PromoCode]:
    return list((await s.scalars(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(30))).all())


async def delete_promo(s: AsyncSession, code: str) -> None:
    await s.execute(delete(PromoCode).where(PromoCode.code == code))


async def redeem_promo(s: AsyncSession, code: str, user_id: int) -> tuple[str, int]:
    """→ (статус, кредиты). Статусы: ok | not_found | used | exhausted."""
    code = code.strip().upper()
    promo = await s.get(PromoCode, code)
    if promo is None:
        return "not_found", 0
    if await s.get(PromoUse, (code, user_id)) is not None:
        return "used", 0
    res = await s.execute(
        update(PromoCode)
        .where(PromoCode.code == code, or_(PromoCode.max_uses == 0, PromoCode.used < PromoCode.max_uses))
        .values(used=PromoCode.used + 1)
    )
    if res.rowcount != 1:
        return "exhausted", 0
    try:
        s.add(PromoUse(code=code, user_id=user_id))
        await s.flush()
    except IntegrityError:
        await s.rollback()
        return "used", 0
    await add_credits(s, user_id, promo.credits)
    return "ok", promo.credits
