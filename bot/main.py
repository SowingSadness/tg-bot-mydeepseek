"""Точка входа бота."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from .compare import api as compare_api
from .compare import service as compare_service
from .compare.api import WidgetAPI
from .compare.store import ComparisonStore
from .config import settings
from .deepseek import deepseek
from .handlers import compare, group, inline, private
from .logging_setup import describe_update, setup_logging
from .respond import close_file_session

log = logging.getLogger(__name__)

#: Как часто фоновая задача чистит кеш сравнений по TTL и по числу записей.
COMPARE_PURGE_INTERVAL = 6 * 3600


async def _warmup() -> None:
    """Прогревает TLS-соединение с DeepSeek, чтобы первый inline-запрос был быстрее."""
    try:
        await deepseek.ask("ok", max_tokens=1)
        log.info("Соединение с DeepSeek прогрето")
    except Exception:  # noqa: BLE001
        log.debug("Прогрев соединения с DeepSeek не удался", exc_info=True)


def commands() -> list[BotCommand]:
    """Команды бота для меню в клиенте (регистрируются при старте)."""
    return [
        BotCommand(command="start", description="О боте"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="cmp", description="Сравнить две картинки (альбом + /cmp)"),
        BotCommand(command="think", description="Режим рассуждений вкл/выкл"),
        BotCommand(command="reset", description="Сбросить настройки"),
    ]


def build_dispatcher() -> Dispatcher:
    """Собирает диспетчер и роутеры.

    Порядок важен: `/cmp` подключается **раньше** `private`, иначе catch-all
    `F.text.startswith("/")` в `private` отвечает «Неизвестная команда» на `/cmp`.
    """
    dispatcher = Dispatcher()
    dispatcher.include_router(inline.router)
    dispatcher.include_router(compare.router)
    dispatcher.include_router(private.router)
    dispatcher.include_router(group.router)

    @dispatcher.update.outer_middleware()
    async def _log_update(handler, event, data):  # noqa: ANN001
        """DEBUG-лог входящего апдейта; на уровне ERROR ничего не пишет."""
        try:
            # `describe_update` собирает строку небесплатно (redact/регекспы), поэтому
            # вызываем его только когда DEBUG реально включён для нашего логгера.
            if log.isEnabledFor(logging.DEBUG):
                log.debug("%s", describe_update(event))
        except Exception:  # noqa: BLE001 — лог не должен ломать обработку
            log.debug("Не удалось описать входящий апдейт", exc_info=True)
        return await handler(event, data)

    @dispatcher.callback_query()
    async def _answer_any_callback(callback) -> None:  # noqa: ANN001
        """Чтобы кнопки не «зависали» со спиннером вне личного чата."""
        await callback.answer()

    return dispatcher


async def setup_compare() -> None:
    """Готовит кеш сравнений и клиент Bot API 10.4.

    Каталог кеша создаётся здесь же: в Docker `/app` принадлежит root, поэтому
    каталог заранее создан в образе (см. Dockerfile).
    """
    if not settings.telegram_bot_token.strip():
        raise RuntimeError("TELEGRAM_BOT_TOKEN пуст — нечего отправлять в Bot API")

    cache_dir = settings.cmp_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    store = ComparisonStore(cache_dir / "cmp.db", id_digits=settings.cmp_id_digits)
    compare_service.configure_store(store)
    compare_api.configure(
        WidgetAPI(
            settings.telegram_bot_token,
            settings.widget_api_base,
            settings.widget_api_test_env,
        )
    )

    removed = await store.purge(
        ttl_seconds=settings.cmp_cache_ttl_days * 86_400,
        max_entries=settings.cmp_cache_max_entries,
    )
    log.info(
        "Кеш сравнений: %s (удалено %d, осталось %d)",
        store.path,
        removed,
        await store.count(),
    )


async def _purge_loop(interval: float = COMPARE_PURGE_INTERVAL) -> None:
    """Раз в `interval` секунд чистит кеш сравнений, чтобы он не рос без рестарта."""
    while True:
        await asyncio.sleep(interval)
        try:
            store = compare_service.get_store()
        except RuntimeError:
            log.debug("Кеш сравнений не настроен — фоновая чистка остановлена")
            return
        try:
            removed = await store.purge(
                ttl_seconds=settings.cmp_cache_ttl_days * 86_400,
                max_entries=settings.cmp_cache_max_entries,
            )
        except Exception:  # noqa: BLE001 — фоновая задача не должна падать навсегда
            log.exception("Не удалось почистить кеш сравнений")
            continue
        if removed:
            log.info("Кеш сравнений: удалено %d старых записей", removed)


async def teardown_compare() -> None:
    """Закрывает клиент Bot API и соединение с кешем; безопасно без настройки."""
    try:
        api = compare_api.get_api()
    except RuntimeError:
        api = None
    if api is not None:
        with suppress(Exception):  # noqa: BLE001 — на остановке важнее дойти до конца
            await api.close()

    try:
        store = compare_service.get_store()
    except RuntimeError:
        store = None
    if store is not None:
        with suppress(Exception):  # noqa: BLE001
            await store.close()


async def _set_commands(bot: Bot) -> None:
    await bot.set_my_commands(commands())


async def main() -> None:
    setup_logging()

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dispatcher = build_dispatcher()
    background: list[asyncio.Task] = []

    # `finally` с `setup_compare`: кеш и HTTP-сессия закрываются, даже если
    # `get_me()` или `start_polling` упали на сети.
    try:
        await setup_compare()

        await _set_commands(bot)
        await bot.delete_webhook(drop_pending_updates=True)

        me = await bot.get_me()
        if (me.username or "").lower() != settings.bot_username.lower():
            log.warning(
                "BOT_USERNAME=%s не совпадает с реальным @%s — упоминания в группах и inline-режим могут не работать.",
                settings.bot_username,
                me.username,
            )
        log.info(
            "Бот @%s запущен (модель %s, BOT_USERNAME=%s)",
            me.username,
            settings.deepseek_model,
            settings.bot_username,
        )

        if settings.warmup_on_start:
            # Не ждём прогрева: он нужен только для скорости первого запроса.
            background.append(asyncio.create_task(_warmup()))
        background.append(asyncio.create_task(_purge_loop()))

        await dispatcher.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with suppress(asyncio.CancelledError):
                await task
        await teardown_compare()
        with suppress(Exception):  # noqa: BLE001 — сессия файлов может быть не создана
            await close_file_session()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
