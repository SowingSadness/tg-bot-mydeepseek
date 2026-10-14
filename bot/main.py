"""Точка входа бота."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from .config import settings
from .handlers import group, inline, private

log = logging.getLogger(__name__)


async def _set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="О боте"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="think", description="Режим рассуждений вкл/выкл"),
            BotCommand(command="reset", description="Сбросить настройки"),
        ]
    )


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(inline.router)
    dispatcher.include_router(private.router)
    dispatcher.include_router(group.router)

    @dispatcher.callback_query()
    async def _answer_any_callback(callback) -> None:  # noqa: ANN001
        """Чтобы кнопки не «зависали» со спиннером вне личного чата."""
        await callback.answer()

    await _set_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен как @%s (модель %s)", settings.bot_username, settings.deepseek_model)

    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
