"""Тесты хендлеров `/cmp`: фильтр чатов, разбор `#номера` и реальная маршрутизация.

Маршрутизация проверяется через настоящий `Dispatcher` с настоящими роутерами
`compare` → `private` → `group` и лёгким фейковым ботом: важно, что `/cmp@bot`
в группе не проваливается в DeepSeek-обработчик `group`, а личка по-прежнему
идёт через `sendWidget`. Сети нет: Bot API и кеш — двойники.
"""

from __future__ import annotations

import logging
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.types import Update
from PIL import Image

from bot.compare import api as api_module
from bot.compare import images, service
from bot.config import settings
from bot.handlers import compare as compare_handlers
from bot.handlers import group as group_handlers
from bot.handlers import private as private_handlers

USER_ID = 42
PRIVATE_CHAT_ID = 777
GROUP_CHAT_ID = -1001234567890


# --------------------------------------------------------------------- helpers


def png(color: tuple[int, int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (40, 30), color).save(buffer, format="PNG")
    return buffer.getvalue()


PNG_A = png((220, 30, 30))
PNG_B = png((30, 30, 220))


class FakeChat:
    def __init__(self, chat_id: int, chat_type) -> None:
        self.id = chat_id
        self.type = chat_type


class FakeUser:
    id = USER_ID
    is_bot = False


class FakeMessage:
    """Минимальный двойник сообщения для прямых вызовов `on_cmp`."""

    def __init__(
        self,
        *,
        chat_type="private",
        chat_id: int = PRIVATE_CHAT_ID,
        text: str | None = None,
        caption: str | None = None,
        photo: list | None = None,
        document=None,
        reply_to_message=None,
    ) -> None:
        self.chat = FakeChat(chat_id, chat_type)
        self.from_user = FakeUser()
        self.text = text
        self.caption = caption
        self.photo = photo
        self.document = document
        self.reply_to_message = reply_to_message
        self.message_id = 1
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:  # noqa: ARG002
        self.answers.append(text)

    async def reply(self, text: str, **kwargs) -> None:  # noqa: ARG002
        self.answers.append(text)


@pytest.fixture
def patched_service(monkeypatch):
    """Все сервисные ветки — AsyncMock'и: проверяем только выбор ветки хендлером."""
    mocked = SimpleNamespace(
        compare_urls=AsyncMock(),
        compare_images=AsyncMock(),
        arm_album=AsyncMock(),
        resend_cached=AsyncMock(),
    )
    for name, mock in vars(mocked).items():
        monkeypatch.setattr(service, name, mock)
    return mocked


# --------------------------------------------------------------------- filter


def _root_filter():
    filters = [item.magic for item in compare_handlers.router.message._handler.filters]
    assert len(filters) == 1
    return filters[0]


def test_cmp_chats_include_groups_but_not_channels() -> None:
    assert compare_handlers.CMP_CHATS == frozenset(
        {ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP}
    )
    assert ChatType.CHANNEL not in compare_handlers.CMP_CHATS

    operation = _root_filter()._operations[-1]
    assert operation.function.__name__ == "in_op"
    assert operation.args == (compare_handlers.CMP_CHATS,)


async def test_router_filter_accepts_groups_and_rejects_channels() -> None:
    root = _root_filter()
    for chat_type in (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP):
        assert root.resolve(FakeMessage(chat_type=chat_type)) is True
    for chat_type in (ChatType.CHANNEL, "channel"):
        assert root.resolve(FakeMessage(chat_type=chat_type)) is False


# --------------------------------------------------------------------- вход


async def test_on_cmp_logs_entry_without_text_or_url(caplog, patched_service) -> None:
    text = "/cmp https://secret.example/a.png?startapp=TOKENVALUE"
    message = FakeMessage(chat_type=ChatType.GROUP, chat_id=GROUP_CHAT_ID, text=text)

    with caplog.at_level(logging.INFO, logger=compare_handlers.log.name):
        await compare_handlers.on_cmp(message)

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == compare_handlers.log.name
    ]
    assert len(lines) == 1, caplog.text
    line = lines[0]
    assert f"chat={GROUP_CHAT_ID}/group" in line
    assert f"user={USER_ID}" in line
    assert f"text={len(text)}" in line
    assert "urls=1" in line
    assert "own_image=False" in line
    assert "reply=False" in line
    for secret in ("secret.example", "startapp", "TOKENVALUE", "https://"):
        assert secret not in line


# --------------------------------------------------------------------- #номер


@pytest.mark.parametrize("chat_type", ["group", "private", ChatType.SUPERGROUP])
async def test_hash_number_calls_resend_cached(chat_type, patched_service) -> None:
    message = FakeMessage(chat_type=chat_type, text="/cmp #337326")

    await compare_handlers.on_cmp(message)

    patched_service.resend_cached.assert_awaited_once_with(message, "337326")
    patched_service.compare_urls.assert_not_called()
    patched_service.compare_images.assert_not_called()
    patched_service.arm_album.assert_not_called()
    assert message.answers == []


async def test_two_links_win_over_hash_number(patched_service) -> None:
    message = FakeMessage(
        text="/cmp #337326 https://a.test/one.png https://b.test/two.png"
    )

    await compare_handlers.on_cmp(message)

    patched_service.compare_urls.assert_awaited_once_with(
        message, ["https://a.test/one.png", "https://b.test/two.png"]
    )
    patched_service.resend_cached.assert_not_called()


async def test_hash_number_wins_over_own_photo(patched_service) -> None:
    message = FakeMessage(
        caption="/cmp #337326",
        photo=[SimpleNamespace(file_id="photo-1", file_size=100)],
    )

    await compare_handlers.on_cmp(message)

    patched_service.resend_cached.assert_awaited_once_with(message, "337326")
    patched_service.arm_album.assert_not_called()
    patched_service.compare_images.assert_not_called()


# --------------------------------------------------------------------- hint


@pytest.mark.parametrize(
    ("chat_type", "privacy"),
    [("group", True), ("supergroup", True), (ChatType.GROUP, True), ("private", False)],
)
async def test_empty_cmp_hint_mentions_privacy_only_in_groups(
    chat_type, privacy
) -> None:
    message = FakeMessage(chat_type=chat_type, text="/cmp")

    await compare_handlers.on_cmp(message)

    assert message.answers == [service._need_two_text(message)]
    assert ("privacy mode" in message.answers[0]) is privacy
    assert ("setprivacy" in message.answers[0]) is privacy


# ----------------------------------------------------------- dispatcher/router


class FakeBot:
    """Фейковый бот: `me()` для `/cmp@username` и запись исходящих методов."""

    id = 424242

    def __init__(self) -> None:
        self.username = settings.bot_username
        self.methods: list = []
        self.photos: list = []

    async def me(self):
        return SimpleNamespace(username=self.username)

    async def __call__(self, method, request_timeout=None):  # noqa: ARG002
        self.methods.append(method)
        name = type(method).__name__
        if name == "SendPhoto":
            sent = SimpleNamespace(
                message_id=900 + len(self.photos),
                photo=[SimpleNamespace(file_id=f"preview-{len(self.photos)}")],
            )
            self.photos.append(sent)
            return sent
        if name == "SendMediaGroup":
            return [SimpleNamespace(message_id=950)]
        return SimpleNamespace(message_id=951)

    async def send_photo(self, chat_id, photo, **kwargs):  # noqa: ARG002
        return SimpleNamespace(
            message_id=900,
            photo=[SimpleNamespace(file_id="preview-file")],
        )

    async def delete_message(self, chat_id, message_id):  # noqa: ARG002
        return True


class FakeStore:
    """Кеш-двойник: номер всегда `337326`."""

    cmp_id = "337326"

    async def save(self, owner_id, **kwargs):  # noqa: ARG002
        return self.cmp_id

    async def set_preview_file_id(self, cmp_id, file_id) -> None:  # noqa: ARG002
        return None

    async def get(self, cmp_id, *, owner_id=None):  # noqa: ARG002
        return None


class FakeAPI:
    """Bot API-двойник: виджеты только записываются."""

    session = object()

    def __init__(self) -> None:
        self.calls: list = []

    async def send_widget(
        self, chat_id, html, *, caption=None, width=0, height=0
    ) -> dict:  # noqa: ARG002
        self.calls.append((chat_id, html, caption))
        return {"message_id": 1}

    async def close(self) -> None:
        return None


@pytest.fixture
def routed(monkeypatch):
    """Настоящий Dispatcher с настоящими роутерами в боевом порядке.

    `Router` можно подключить только к одному родителю, а `test_wiring` строит
    боевой диспетчер из тех же объектов. Поэтому родитель временно снимается
    и восстанавливается после теста — так оба теста не зависят от порядка.
    """
    routers = (compare_handlers.router, private_handlers.router, group_handlers.router)
    parents = {router: router.parent_router for router in routers}
    for router in routers:
        router._parent_router = None

    dispatcher = Dispatcher()
    dispatcher.include_router(compare_handlers.router)
    dispatcher.include_router(private_handlers.router)
    dispatcher.include_router(group_handlers.router)
    bot = FakeBot()

    yield dispatcher, bot

    for router, parent in parents.items():
        router._parent_router = parent


def _update(
    chat_type,
    *,
    text: str | None = None,
    caption: str | None = None,
    photo: bool = False,
    chat_id: int | None = None,
    message_id: int = 1,
) -> Update:
    kind = getattr(chat_type, "value", chat_type)
    chat = {
        "id": chat_id if chat_id is not None else (PRIVATE_CHAT_ID if kind == "private" else GROUP_CHAT_ID),
        "type": kind,
    }
    if kind != "private":
        chat["title"] = "Test group"
    message = {
        "message_id": message_id,
        "date": 1700000000,
        "chat": chat,
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Tester", "username": "tester"},
    }
    if text is not None:
        message["text"] = text
    if caption is not None:
        message["caption"] = caption
    if photo:
        message["photo"] = [
            {"file_id": "photo-1", "file_unique_id": "unique-1", "width": 40, "height": 30}
        ]
    return Update.model_validate({"update_id": 1, "message": message})


async def test_group_cmp_photo_reaches_compare_not_deepseek(routed, monkeypatch) -> None:
    dispatcher, bot = routed
    arm_album = AsyncMock()
    download_photo = AsyncMock()
    deepseek = AsyncMock()
    monkeypatch.setattr(service, "arm_album", arm_album)
    monkeypatch.setattr(group_handlers, "download_photo", download_photo)
    monkeypatch.setattr(group_handlers, "ask_and_reply", deepseek)

    update = _update("group", caption=f"/cmp@{settings.bot_username}", photo=True)
    await dispatcher.feed_update(bot, update)

    arm_album.assert_awaited_once()
    handled = arm_album.await_args.args[0]
    assert handled.chat.type == "group"
    assert handled.caption == f"/cmp@{settings.bot_username}"
    # DeepSeek-обработчик группы не получил ни скачивания, ни запроса.
    download_photo.assert_not_awaited()
    deepseek.assert_not_awaited()


@pytest.mark.parametrize("chat_type", ["group", "private"])
async def test_two_links_reach_real_service_and_pick_send_branch(
    routed, monkeypatch, chat_type
) -> None:
    dispatcher, bot = routed
    store = FakeStore()
    api = FakeAPI()
    service.configure_store(store)  # type: ignore[arg-type]
    api_module.configure(api)  # type: ignore[arg-type]
    monkeypatch.setattr(service.originals, "save", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        images, "download_url", AsyncMock(side_effect=[PNG_A, PNG_B])
    )
    deepseek = AsyncMock()
    monkeypatch.setattr(group_handlers, "download_photo", AsyncMock())
    monkeypatch.setattr(group_handlers, "ask_and_reply", deepseek)
    update = _update(
        chat_type,
        text=f"/cmp@{settings.bot_username} https://a.test/one.png https://b.test/two.png",
    )

    try:
        await dispatcher.feed_update(bot, update)
    finally:
        service.configure_store(None)  # type: ignore[arg-type]
        api_module.configure(None)  # type: ignore[arg-type]

    albums = [method for method in bot.methods if type(method).__name__ == "SendMediaGroup"]
    texts = [method for method in bot.methods if type(method).__name__ == "SendMessage"]

    if chat_type == "group":
        # В группе виджет не пробуем: сразу JPEG-альбом и номер текстом.
        assert api.calls == [], "в группе sendWidget вызываться не должен"
        assert len(albums) == 1
        assert [item.type for item in albums[0].media] == ["photo", "photo"]
        assert [item.media.filename for item in albums[0].media] == [
            "before.jpg",
            "after.jpg",
        ]
        assert any("#337326" in method.text for method in texts)
    else:
        # Личка не изменилась: бабл через sendWidget, без медиагруппы.
        assert len(api.calls) == 1
        assert "#337326" in (api.calls[0][2] or "")
        assert albums == []

    assert deepseek.await_count == 0


async def test_plain_group_mention_still_goes_to_deepseek(routed, monkeypatch) -> None:
    dispatcher, bot = routed
    deepseek = AsyncMock()
    monkeypatch.setattr(group_handlers, "ask_and_reply", deepseek)
    update = _update("group", text=f"@{settings.bot_username} привет")

    await dispatcher.feed_update(bot, update)

    deepseek.assert_awaited_once()
    assert deepseek.await_args.args[1] == "привет"
