"""Точка входа: `python main.py` (или `python run.py`) запускает Telegram-бота."""
import asyncio
import logging

from bot import build, setup
from config import settings
from db.session import engine, init_db
from services import billing

log = logging.getLogger("main")


async def run() -> None:
    await init_db()
    await billing.reload()
    bot, dp = build()
    try:
        me = await bot.get_me()  # fail fast: сразу видно, если BOT_TOKEN неверный
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram не принял BOT_TOKEN (%s). Проверьте токен: python run.py --reconfigure", exc)
        await bot.session.close()
        raise SystemExit(1) from exc
    log.info("Бот запущен: @%s | админов: %d", me.username, len(settings.admin_set))
    await setup(bot)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
