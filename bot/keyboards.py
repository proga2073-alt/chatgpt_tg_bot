from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.utils import btn, grid, kb
from config import settings


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [btn("💬 Диалоги", "m:chats"), btn("🆕 Новый диалог", "m:new")],
        [btn("🤖 Модель", "s:model"), btn("⚙️ Настройки", "m:settings")],
        [btn("⭐ Баланс", "m:bal"), btn("🎭 Роли", "s:persona")],
        [btn("📂 Файлы", "m:files"), btn("❓ Помощь", "m:help")],
    ]
    if is_admin:
        rows.append([btn("🛠 Админ-панель", "a:menu")])
    return kb(*rows)


def buy_kb() -> InlineKeyboardMarkup:
    packs = [btn(f"{n} ⭐", f"buy:{n}") for n in settings.star_pack_list]
    return kb(*grid(packs, 2), [btn("🎟 Промокод", "b:promo")], [btn("◀️ Меню", "m:menu")])
