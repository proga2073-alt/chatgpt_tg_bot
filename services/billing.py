"""Цены моделей. Цена сообщения = множитель × MESSAGE_BASE_CREDITS; бесплатные модели — 0.

Значения из .env можно переопределить в админ-панели (таблица model_configs, кэш в памяти).
"""
from __future__ import annotations

from math import ceil
from typing import Any

from config import settings
from db import repo
from db.session import SessionLocal

_over: dict[str, dict[str, Any]] = {}


async def reload() -> None:
    """Перечитывает правки администратора из БД."""
    async with SessionLocal() as s:
        rows = await repo.list_model_configs(s)
    _over.clear()
    _over.update({r.model: {"enabled": r.enabled, "free": r.free, "multiplier": r.multiplier} for r in rows})


def override(model: str) -> dict[str, Any] | None:
    return _over.get(model)


def is_enabled(model: str) -> bool:
    return bool(_over.get(model, {}).get("enabled", True))


def is_free(model: str) -> bool:
    flag = _over.get(model, {}).get("free")
    return model in settings.free_model_set if flag is None else bool(flag)


def multiplier(model: str) -> float:
    value = _over.get(model, {}).get("multiplier")
    if value is not None:
        return float(value)
    return settings.model_multipliers.get(model, settings.default_multiplier)


def cost_credits(model: str) -> int:
    """Стоимость одного сообщения в кредитах (0 — бесплатно)."""
    if is_free(model):
        return 0
    return max(1, ceil(round(multiplier(model) * settings.message_base_credits, 6)))


def stars(credits: int) -> float:
    return credits / settings.credits_per_star


def fmt_stars(credits: int) -> str:
    return f"{round(stars(credits), 1):g} ⭐"


def price_label(model: str) -> str:
    return "бесплатно" if is_free(model) else fmt_stars(cost_credits(model))
