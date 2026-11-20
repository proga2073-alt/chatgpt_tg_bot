from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonCommands

from bot.handlers import admin, agent, billing, chat, chats, common, prefs
from bot.middlewares import CommandResetMiddleware, GuardMiddleware
from config import settings

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="new", description="Новый диалог"),
    BotCommand(command="chats", description="Мои диалоги"),
    BotCommand(command="model", description="Выбрать модель"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="persona", description="Роли ассистента"),
    BotCommand(command="files", description="Мои файлы"),
    BotCommand(command="balance", description="Баланс ⭐"),
    BotCommand(command="buy", description="Пополнить баланс"),
    BotCommand(command="promo", description="Ввести промокод"),
    BotCommand(command="export", description="Выгрузить диалог"),
    BotCommand(command="stats", description="Моя статистика"),
    BotCommand(command="help", description="Справка"),
]


def build() -> tuple[Bot, Dispatcher]:
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()  # FSM-хранилище по умолчанию — в памяти
    dp.message.filter(F.chat.type == "private")
    dp.message.outer_middleware(CommandResetMiddleware())
    for observer in (dp.message, dp.callback_query, dp.pre_checkout_query):
        observer.outer_middleware(GuardMiddleware())
    # порядок важен: универсальный обработчик текста (chat) — последним
    dp.include_routers(common.router, chats.router, prefs.router, billing.router, admin.router, agent.router, chat.router)
    return bot, dp


async def setup(bot: Bot) -> None:
    """Команды и меню. Ошибки не критичны."""
    try:
        await bot.set_my_commands(USER_COMMANDS)
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())  # убирает старую кнопку Mini App
        for admin_id in settings.admin_set:
            await bot.set_my_commands(
                [*USER_COMMANDS, BotCommand(command="admin", description="Админ-панель")],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Bot setup skipped: %s", exc)
