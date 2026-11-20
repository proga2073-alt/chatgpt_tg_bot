"""ORM-модели. Совместимы и с SQLite, и с PostgreSQL."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, false, true
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_TITLE = "Новый диалог"


def utcnow() -> datetime:
    """Наивное UTC-время (так хранится и в SQLite, и в PostgreSQL TIMESTAMP)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # Telegram user id
    username: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(200))  # None → OPENAI_MODEL
    system_prompt: Mapped[str | None] = mapped_column(Text)  # None → дефолтный промт
    temperature: Mapped[float] = mapped_column(Float, default=1.0)
    max_tokens: Mapped[int | None] = mapped_column(Integer)  # None → без ограничения
    balance: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # кредиты (10 кредитов = 1 ⭐)
    banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    current_conversation_id: Mapped[int | None] = mapped_column(Integer)  # активный диалог для чата в Telegram
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default=DEFAULT_TITLE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Usage(Base):
    """Счётчик запросов в сутки (для необязательного дневного лимита)."""

    __tablename__ = "usage"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD, UTC
    count: Mapped[int] = mapped_column(Integer, default=0)


class Payment(Base):
    """Журнал пополнений в Telegram Stars. charge_id уникален — защита от повторного зачисления."""

    __tablename__ = "payments"

    charge_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # telegram_payment_charge_id
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    stars: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    refunded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ModelConfig(Base):
    """Правки администратора поверх .env: скрыть модель, сделать бесплатной/платной, свой множитель."""

    __tablename__ = "model_configs"

    model: Mapped[str] = mapped_column(String(200), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    free: Mapped[bool | None] = mapped_column(Boolean)  # None → как в FREE_MODELS
    multiplier: Mapped[float | None] = mapped_column(Float)  # None → как в MODEL_MULTIPLIERS


class PromoCode(Base):
    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    credits: Mapped[int] = mapped_column(Integer)
    max_uses: Mapped[int] = mapped_column(Integer, default=0)  # 0 = без ограничения
    used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PromoUse(Base):
    """Один пользователь — один раз на промокод."""

    __tablename__ = "promo_uses"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
