"""Тесты проводки: порядок роутеров, команды, справка, старт/остановка кеша и API.

Проверяется именно то, что легко сломать незаметно: `/cmp` должен попадать в
`compare`-роутер раньше catch-all в `private`, кеш и клиент Bot API должны
создаваться на старте и закрываться на остановке. Сети нет: Bot API — фейк,
Store — настоящий SQLite во временном каталоге.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from contextlib import suppress
from io import BytesIO
from types import SimpleNamespace

import aiohttp
import pytest
from PIL import Image

from bot import main as main_module
from bot.compare import api as api_module
from bot.compare import service as service_module
from bot.compare.api import WidgetAPI
from bot.compare.store import ComparisonStore
from bot.config import settings
from bot.handlers import private


# --------------------------------------------------------------------- helpers


def png(color: tuple[int, int, int], size: tuple[int, int] = (240, 180)) -> bytes:
    """Настоящая PNG-картинка: `images.load` и `build.build` работают по-настоящему."""
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeAPI:
    """Двойник `WidgetAPI`: запоминает отправленные виджеты."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.session = object()

    async def send_widget(
        self,
        chat_id: int,
        html: str,
        *,
        caption: str | None = None,
        width: int = 0,
        height: int = 0,
    ) -> dict:
        self.calls.append(("send_widget", chat_id, html, caption, width, height))
        return {"message_id": 1}

    async def close(self) -> None:  # как у настоящего клиента
        return None


class FakeMessage:
    """Минимальный двойник сообщения Telegram."""

    def __init__(self, user_id: int = 42, chat_id: int = 777) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.message_id = 1
        self.answers: list[str] = []
        self.media_groups: list = []

    async def answer(self, text: str, **kwargs) -> None:  # noqa: ARG002
        self.answers.append(text)

    async def reply(self, text: str, **kwargs) -> None:  # noqa: ARG002
        self.answers.append(text)

    async def answer_media_group(self, media, **kwargs) -> None:  # noqa: ARG002
        self.media_groups.append(media)


# --------------------------------------------------------------------- тесты

_DISPATCHER = None


def _dispatcher():
    """Один диспетчер на модуль: aiogram запрещает подключать роутеры дважды."""
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = main_module.build_dispatcher()
    return _DISPATCHER


def test_compare_router_goes_before_private() -> None:
    """Иначе catch-all `F.text.startswith("/")` в private съест /cmp."""
    dispatcher = _dispatcher()
    assert [router.name for router in dispatcher.sub_routers] == [
        "inline",
        "compare",
        "private",
        "group",
    ]


def _middleware():
    """Внешний middleware `_log_update` с собранного диспетчера."""
    dispatcher = _dispatcher()
    return next(
        item
        for item in dispatcher.update.outer_middleware
        if getattr(item, "__name__", "") == "_log_update"
    )


def _update_event(**overrides) -> SimpleNamespace:
    """Минимальный апдейт: по умолчанию текстовое сообщение с update_id=5."""
    message = {
        "chat": SimpleNamespace(id=777, type="private"),
        "from_user": SimpleNamespace(id=42, username="tester"),
        "message_id": 1,
        "text": "привет",
        "caption": None,
        "photo": None,
        "document": None,
        "media_group_id": None,
    }
    message.update(overrides)
    return SimpleNamespace(
        update_id=5, event_type="message", message=SimpleNamespace(**message)
    )


def _records(caplog, level: int) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == level]


async def test_update_middleware_logs_compact_info(caplog) -> None:
    """INFO — одна компактная строка: контекст и текст, без `file_id`."""
    middleware = _middleware()
    seen: list[tuple] = []

    async def handler(event, data):  # noqa: ANN001
        seen.append((event, data))
        return "handled"

    event = _update_event()
    with caplog.at_level(logging.INFO, logger=main_module.log.name):
        assert await middleware(handler, event, {"bot": None}) == "handled"

    assert seen and seen[0][0] is event
    info = _records(caplog, logging.INFO)
    assert len(info) == 1, caplog.text
    line = info[0].getMessage()
    assert "update=message" in line
    assert "chat_id=777" in line
    assert "chat_type=private" in line
    assert "user_id=42" in line
    assert "text='привет'" in line
    assert "file_id" not in line
    assert "\n" not in line


async def test_update_middleware_logs_verbose_debug(caplog) -> None:
    """DEBUG дополнительно печатает `file_id`, размер и `media_group_id`."""
    middleware = _middleware()

    async def handler(event, data):  # noqa: ANN001
        return "handled"

    photo = SimpleNamespace(
        file_id="file-photo", width=100, height=80, file_size=12345
    )
    event = _update_event(photo=[photo], media_group_id="album-1")
    with caplog.at_level(logging.DEBUG, logger=main_module.log.name):
        assert await middleware(handler, event, {}) == "handled"

    debug = _records(caplog, logging.DEBUG)
    assert debug, caplog.text
    line = debug[-1].getMessage()
    assert "media=photo" in line
    assert "file_id=file-photo" in line
    assert "size=100x80" in line
    assert "bytes=12345" in line
    assert "media_group_id=album-1" in line
    # INFO-строка тоже есть, но без медиа-деталей.
    info = _records(caplog, logging.INFO)
    assert info and "file_id" not in info[0].getMessage()
    assert "\n" not in line
    assert "\n" not in info[0].getMessage()


async def test_update_middleware_warns_about_slow_update(caplog, monkeypatch) -> None:
    """Апдейт дольше 3 c — `WARNING update <id>: slow <N>s`, без текста сообщения."""
    middleware = _middleware()
    ticks = iter([100.0, 104.2])
    monkeypatch.setattr(main_module.time, "perf_counter", lambda: next(ticks))

    async def handler(event, data):  # noqa: ANN001
        return "handled"

    with caplog.at_level(logging.WARNING, logger=main_module.log.name):
        assert await middleware(handler, _update_event(), {}) == "handled"

    warnings = _records(caplog, logging.WARNING)
    assert [record.getMessage() for record in warnings] == ["update 5: slow 4.2s"]
    assert "привет" not in caplog.text


async def test_update_middleware_fast_update_is_not_slow(caplog, monkeypatch) -> None:
    """Быстрый апдейт под порог 3 c не даёт WARNING."""
    middleware = _middleware()
    ticks = iter([100.0, 102.9])
    monkeypatch.setattr(main_module.time, "perf_counter", lambda: next(ticks))

    async def handler(event, data):  # noqa: ANN001
        return "handled"

    with caplog.at_level(logging.WARNING, logger=main_module.log.name):
        assert await middleware(handler, _update_event(), {}) == "handled"

    assert _records(caplog, logging.WARNING) == []


async def test_update_middleware_logs_crash_and_reraises(caplog) -> None:
    """Исключение в хендлере — `ERROR` с `exc_info`, и оно не проглатывается."""
    middleware = _middleware()

    async def handler(event, data):  # noqa: ANN001
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR, logger=main_module.log.name):
        with pytest.raises(ValueError, match="boom"):
            await middleware(handler, _update_event(), {})

    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 1, caplog.text
    assert errors[0].getMessage() == "update 5: обработчик упал"
    assert errors[0].exc_info is not None
    assert "ValueError: boom" in caplog.text


def test_commands_include_cmp() -> None:
    commands = main_module.commands()
    assert [item.command for item in commands] == [
        "start",
        "help",
        "cmp",
        "think",
        "reset",
    ]
    for item in commands:
        assert item.description
        assert len(item.description) <= 256


def test_help_mentions_cmp_and_number() -> None:
    assert "/cmp" in private.HELP_TEXT
    assert "две картинки" in private.HELP_TEXT
    assert f"@{settings.bot_username} #" in private.HELP_TEXT


async def test_setup_compare_creates_cache_and_configures(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "cmp_cache_dir", tmp_path / "cmp")

    await main_module.setup_compare()
    try:
        assert (tmp_path / "cmp" / "cmp.db").exists()
        store = service_module.get_store()
        assert isinstance(store, ComparisonStore)
        assert await store.count() == 0
        assert isinstance(api_module.get_api(), WidgetAPI)
    finally:
        await main_module.teardown_compare()

    # Повторный старт после остановки не падает.
    await main_module.setup_compare()
    await main_module.teardown_compare()


async def test_setup_compare_logs_main_mini_app_link(
    tmp_path, monkeypatch, caplog
) -> None:
    """Пустой `CMP_WEBAPP_SHORT_NAME` — Main Mini App, в логе форма без short name."""
    monkeypatch.setattr(settings, "cmp_cache_dir", tmp_path / "cmp")
    monkeypatch.setattr(settings, "cmp_webapp_short_name", "")

    with caplog.at_level(logging.INFO, logger=main_module.log.name):
        await main_module.setup_compare()
    try:
        assert f"https://t.me/{settings.bot_username}?startapp=…" in caplog.text
        assert "Main Mini App" in caplog.text
    finally:
        await main_module.teardown_compare()


async def test_setup_compare_logs_short_name_link(tmp_path, monkeypatch, caplog) -> None:
    """Заданный `CMP_WEBAPP_SHORT_NAME` — ссылка с `/<short>` и без пометки Main Mini App."""
    monkeypatch.setattr(settings, "cmp_cache_dir", tmp_path / "cmp")
    monkeypatch.setattr(settings, "cmp_webapp_short_name", "cmpapp")

    with caplog.at_level(logging.INFO, logger=main_module.log.name):
        await main_module.setup_compare()
    try:
        assert f"https://t.me/{settings.bot_username}/cmpapp?startapp=…" in caplog.text
        assert "Main Mini App" not in caplog.text
    finally:
        await main_module.teardown_compare()


async def test_setup_compare_purge_removes_originals(tmp_path, monkeypatch) -> None:
    """`purge` при старте собирает мусор вместе с файлами оригиналов."""
    cache = tmp_path / "cmp"
    monkeypatch.setattr(settings, "cmp_cache_dir", cache)

    # Запись, созданная до старта: при TTL=0 она сразу считается устаревшей.
    seed = ComparisonStore(cache / "cmp.db")
    cmp_id = await seed.save(
        42,
        image_a=b"a",
        image_b=b"b",
        width=100,
        quality=50,
        labels=("До", "После"),
        zlib_bytes=10,
    )
    await seed.close()

    removed: list[str] = []
    monkeypatch.setattr(
        main_module.originals,
        "remove",
        lambda cache_dir, cmp_id: removed.append(cmp_id),
    )
    monkeypatch.setattr(settings, "cmp_cache_ttl_days", 0)

    await main_module.setup_compare()
    try:
        assert removed == [cmp_id], "originals.remove должен дойти до удалённого номера"
        assert await service_module.get_store().count() == 0
    finally:
        await main_module.teardown_compare()


async def test_setup_webapp_disabled_when_port_zero(monkeypatch) -> None:
    """`CMP_WEBAPP_PORT=0` — сервер не поднимается, поведение бота прежнее."""
    monkeypatch.setattr(settings, "cmp_webapp_port", 0)
    assert await main_module.setup_webapp() is None
    # Гашение `None` (сервер не стартовал) безопасно.
    await main_module.teardown_webapp(None)


def _free_port() -> int:
    """Свободный порт loopback: bind(0) → номер → закрыть сокет."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_setup_webapp_serves_healthz_and_stops(tmp_path, monkeypatch) -> None:
    """Свободный порт: `setup_webapp` поднимает сервер, `/healthz` отвечает, teardown гасит."""
    monkeypatch.setattr(settings, "cmp_cache_dir", tmp_path / "cmp")
    port = _free_port()
    monkeypatch.setattr(settings, "cmp_webapp_port", port)
    monkeypatch.setattr(settings, "cmp_webapp_host", "127.0.0.1")

    await main_module.setup_compare()
    runner = await main_module.setup_webapp()
    try:
        assert runner is not None
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/healthz") as response:
                assert response.status == 200
                assert await response.text() == "ok"
    finally:
        await main_module.teardown_webapp(runner)
        await main_module.teardown_webapp(runner)  # повторный teardown безопасен
        await main_module.teardown_compare()

    # После остановки порт больше не слушается.
    with pytest.raises(OSError):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()


async def test_teardown_compare_is_safe_without_setup() -> None:
    await main_module.teardown_compare()
    await main_module.teardown_compare()


async def test_purge_loop_cancels_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "cmp_cache_dir", tmp_path / "cmp")
    await main_module.setup_compare()
    try:
        task = asyncio.create_task(main_module._purge_loop(interval=0.01))
        await asyncio.sleep(0.03)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()
    finally:
        await main_module.teardown_compare()


async def test_compare_images_uses_real_store(tmp_path) -> None:
    """Связка service + настоящий SQLite-кеш + настоящий Pillow, Bot API — фейк."""
    store = ComparisonStore(tmp_path / "cmp.db")
    fake_api = FakeAPI()
    service_module.configure_store(store)
    api_module.configure(fake_api)  # type: ignore[arg-type]
    try:
        message = FakeMessage()
        await service_module.compare_images(
            message, png((220, 40, 40)), png((40, 40, 200))
        )

        assert await store.count() == 1
        assert fake_api.calls, "виджет должен быть отправлен"
        _, chat_id, _, caption, width, height = fake_api.calls[0]
        assert chat_id == message.chat.id
        assert caption and "#" in caption
        assert (width, height) == (0, 0)
        assert any("#" in text for text in message.answers), "номер должен дойти текстом"

        match = re.search(r"#(\d{%d,8})" % settings.cmp_id_digits, caption)
        assert match, caption
        cached = await store.get(match.group(1))
        assert cached is not None
        assert cached.owner_id == message.from_user.id
        assert cached.image_a and cached.image_b
        # Пропорции бабла сохраняются в кеше: inline пересоберёт старый номер 1:1.
        assert cached.aspect == settings.cmp_aspect == "3:2"
    finally:
        service_module.configure_store(None)  # type: ignore[arg-type]
        api_module.configure(None)  # type: ignore[arg-type]
        await store.close()
