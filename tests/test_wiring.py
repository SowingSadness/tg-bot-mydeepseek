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
from contextlib import suppress
from io import BytesIO
from types import SimpleNamespace

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


async def test_update_middleware_logs_and_calls_handler(caplog) -> None:
    """DEBUG-лог апдейта не заменяет обработчик и не падает на его вызове."""
    dispatcher = _dispatcher()
    middleware = next(
        item
        for item in dispatcher.update.outer_middleware
        if getattr(item, "__name__", "") == "_log_update"
    )

    seen: list[tuple] = []

    async def handler(event, data):  # noqa: ANN001
        seen.append((event, data))
        return "handled"

    event = SimpleNamespace(
        event_type="message",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=777, type="private"),
            from_user=SimpleNamespace(id=42, username="tester"),
            message_id=1,
            text="привет",
            caption=None,
            photo=None,
            document=None,
            media_group_id=None,
        ),
    )

    caplog.set_level(logging.DEBUG)
    assert await middleware(handler, event, {"bot": None}) == "handled"
    assert seen and seen[0][0] is event
    assert "chat_id=777" in caplog.text
    assert "user_id=42" in caplog.text
    assert "text='привет'" in caplog.text


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
