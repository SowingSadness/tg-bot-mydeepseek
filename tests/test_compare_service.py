"""Тесты сценариев `/cmp` и роутера — без сети и без реального Telegram.

Сеть подменяется на уровне модулей (`images.download_url`, `respond.download_photo`),
кеш и Bot API — лёгкие фейки через `configure_store` / `api.configure`.
Pillow работает по-настоящему: `images.load` + `build.build` на крошечных PNG.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from bot.compare import api as api_module
from bot.compare import build, images, service
from bot.compare.api import WidgetAPIError
from bot.config import settings
from bot.handlers import compare as compare_handlers

USER_ID = 7
OTHER_USER_ID = 8
CHAT_ID = 100
CMP_ID = "482913"


# --------------------------------------------------------------------- helpers


def png(color: tuple[int, int, int] = (220, 30, 30), size: tuple[int, int] = (40, 30)) -> bytes:
    """Крошечная настоящая PNG-картинка для `images.load`."""
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


PNG_A = png((200, 20, 20))
PNG_B = png((20, 20, 200))


class FakeChat:
    id = CHAT_ID


class FakeUser:
    def __init__(self, user_id: int = USER_ID) -> None:
        self.id = user_id


class FakeMessage:
    """Минимальный двойник `aiogram.types.Message` со записью вызовов."""

    def __init__(
        self,
        calls: list,
        *,
        user_id: int = USER_ID,
        text: str | None = None,
        caption: str | None = None,
        photo: list | None = None,
        document=None,
        reply_to_message=None,
    ) -> None:
        self.calls = calls
        self.chat = FakeChat()
        self.from_user = FakeUser(user_id)
        self.text = text
        self.caption = caption
        self.photo = photo
        self.document = document
        self.reply_to_message = reply_to_message
        self.message_id = 1
        self.bot = None
        self.answers: list[str] = []
        self.media_groups: list = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)
        self.calls.append(("answer", text))

    async def reply(self, text: str, **kwargs) -> None:
        self.answers.append(text)
        self.calls.append(("reply", text))

    async def answer_media_group(self, media, **kwargs) -> None:
        self.media_groups.append(media)
        self.calls.append(("answer_media_group", media))


def photo_message(calls: list, file_id: str = "photo-1") -> FakeMessage:
    return FakeMessage(calls, photo=[SimpleNamespace(file_id=file_id, file_size=100)])


class FakeStore:
    """Кеш-двойник: считает сохранения и возвращает фиксированный номер."""

    def __init__(self, calls: list, cmp_id: str = CMP_ID) -> None:
        self.calls = calls
        self.cmp_id = cmp_id

    async def save(self, owner_id: int, **kwargs) -> str:
        self.calls.append(("store.save", owner_id, kwargs))
        return self.cmp_id

    async def get(self, cmp_id: str, *, owner_id: int | None = None):
        return None


class FakeAPI:
    """Bot API-двойник: пишет отправки в общий список, умеет падать."""

    def __init__(self, calls: list, error: WidgetAPIError | None = None) -> None:
        self.calls = calls
        self.error = error
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
        self.calls.append(("api.send_widget", chat_id, html, caption, width, height))
        if self.error is not None:
            raise self.error
        return {"message_id": 1}


@pytest.fixture
def env(monkeypatch):
    """Свежие фейки кеша/API и пустое ожидание на каждый тест."""
    calls: list = []
    store = FakeStore(calls)
    fake_api = FakeAPI(calls)
    service.configure_store(store)
    api_module.configure(fake_api)
    service._pending.clear()

    yield SimpleNamespace(calls=calls, store=store, api=fake_api)

    service._pending.clear()
    service.configure_store(None)
    api_module.configure(None)


def _names(calls: list) -> list[str]:
    return [call[0] for call in calls]


def _saves(calls: list) -> list:
    return [call for call in calls if call[0] == "store.save"]


def _widgets(calls: list) -> list:
    return [call for call in calls if call[0] == "api.send_widget"]


# -------------------------------------------------------------- downloading


async def test_compare_urls_downloads_both_with_settings(env, monkeypatch):
    seen: list[tuple] = []

    async def fake_download(session, url, *, max_bytes, timeout, allow_private):
        seen.append((session, url, max_bytes, timeout, allow_private))
        return PNG_A if "one" in url else PNG_B

    monkeypatch.setattr(images, "download_url", fake_download)

    message = FakeMessage(
        env.calls,
        text="/cmp https://a.test/one.jpg, https://b.test/two.jpg",
    )
    await compare_handlers.on_cmp(message)

    assert [item[1] for item in seen] == [
        "https://a.test/one.jpg",
        "https://b.test/two.jpg",
    ]
    for session, _url, max_bytes, timeout, allow_private in seen:
        assert session is env.api.session
        assert max_bytes == settings.cmp_max_bytes
        assert timeout == 15.0
        assert allow_private == settings.cmp_allow_private_hosts
    assert len(_saves(env.calls)) == 1
    assert len(_widgets(env.calls)) == 1


async def test_one_url_plus_reply_photo_compares_immediately(env, monkeypatch):
    downloaded: list[str] = []
    urls: list[str] = []

    async def fake_download_photo(message, file_id):
        downloaded.append(file_id)
        return PNG_A

    async def fake_download_url(session, url, *, max_bytes, timeout, allow_private):
        urls.append(url)
        return PNG_B

    monkeypatch.setattr("bot.respond.download_photo", fake_download_photo)
    monkeypatch.setattr(images, "download_url", fake_download_url)

    reply = photo_message(env.calls, "reply-photo")
    message = FakeMessage(
        env.calls,
        text="/cmp https://b.test/two.jpg",
        reply_to_message=reply,
    )
    await compare_handlers.on_cmp(message)

    saves = _saves(env.calls)
    assert downloaded == ["reply-photo"]
    assert urls == ["https://b.test/two.jpg"]
    assert len(saves) == 1
    # В кеш уходят пережатые WebP (а не исходные PNG).
    saved = saves[0][2]
    assert saved["image_a"].startswith(b"RIFF")
    assert saved["image_b"].startswith(b"RIFF")
    assert saved["image_a"] != saved["image_b"]
    assert len(_widgets(env.calls)) == 1
    # Ожидание не открывалось: сравнение уже отправлено.
    assert service.is_waiting(USER_ID) is False


async def test_cmp_without_arguments_starts_empty_waiting(env):
    message = FakeMessage(env.calls, text="/cmp")
    await compare_handlers.on_cmp(message)

    assert service.is_waiting(USER_ID) is True
    assert any("/cmp https://" in answer for answer in message.answers)
    assert not _saves(env.calls)


async def test_cmp_with_single_url_waits_for_second(env, monkeypatch):
    async def fake_download_url(session, url, *, max_bytes, timeout, allow_private):
        return PNG_A

    monkeypatch.setattr(images, "download_url", fake_download_url)
    message = FakeMessage(env.calls, text="/cmp https://a.test/one.jpg")
    await compare_handlers.on_cmp(message)

    state, _ = service._state(USER_ID)
    assert state is not None and state.first == PNG_A
    assert any("1/2" in answer for answer in message.answers)
    assert not _saves(env.calls)


async def test_compare_urls_error_reports_and_saves_nothing(env, monkeypatch):
    async def fake_download(session, url, *, max_bytes, timeout, allow_private):
        raise images.ImageError("Не удалось скачать картинку: сайт не ответил вовремя.")

    monkeypatch.setattr(images, "download_url", fake_download)
    message = FakeMessage(env.calls)
    await service.compare_urls(message, ["https://a.test/one.jpg", "https://b.test/two.jpg"])

    assert message.answers == ["Не удалось скачать картинку: сайт не ответил вовремя."]
    assert not _saves(env.calls)
    assert not _widgets(env.calls)


# -------------------------------------------------------------- two images


async def test_two_images_save_once_and_clear_state(env, monkeypatch):
    downloaded: list[str] = []

    async def fake_download_photo(message, file_id):
        downloaded.append(file_id)
        return PNG_A if file_id == "photo-1" else PNG_B

    monkeypatch.setattr("bot.respond.download_photo", fake_download_photo)

    service.start_empty(USER_ID)
    first = photo_message(env.calls, "photo-1")
    second = photo_message(env.calls, "photo-2")
    await service.add_image(first)
    await service.add_image(second)

    saves = _saves(env.calls)
    assert downloaded == ["photo-1", "photo-2"]
    assert len(saves) == 1
    # В кеш уходят пережатые WebP, ровно одна пара и в правильном порядке.
    saved = saves[0][2]
    assert saved["image_a"].startswith(b"RIFF")
    assert saved["image_b"].startswith(b"RIFF")
    assert saved["image_a"] != saved["image_b"]
    assert service.is_waiting(USER_ID) is False
    assert any("1/2" in answer for answer in first.answers)


async def test_expired_ttl_clears_state_and_asks_to_restart(env, monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(service, "_now", lambda: now)

    service.start_empty(USER_ID)
    assert service.is_waiting(USER_ID) is True

    now += settings.cmp_pending_ttl + 1
    message = photo_message(env.calls)
    await service.add_image(message)

    assert service.is_waiting(USER_ID) is False
    assert message.answers == [service.EXPIRED_TEXT]
    assert not _saves(env.calls)


async def test_is_waiting_only_for_this_user(env):
    service.start_empty(USER_ID)

    assert service.is_waiting(USER_ID) is True
    assert service.is_waiting(OTHER_USER_ID) is False


async def test_add_image_without_waiting_does_nothing(env, monkeypatch):
    called = False

    async def fake_download_photo(message, file_id):
        nonlocal called
        called = True
        return PNG_A

    monkeypatch.setattr("bot.respond.download_photo", fake_download_photo)
    message = photo_message(env.calls)
    await service.add_image(message)

    assert called is False
    assert message.answers == []


# -------------------------------------------------------------- caption/cache


async def test_caption_contains_id_and_save_goes_before_send(env):
    message = FakeMessage(env.calls)
    await service.compare_images(message, PNG_A, PNG_B)

    widgets = _widgets(env.calls)
    assert len(widgets) == 1
    assert f"#{CMP_ID}" in widgets[0][3]
    assert widgets[0][1] == CHAT_ID
    assert widgets[0][4] == 0 and widgets[0][5] == 0

    names = _names(env.calls)
    assert names.index("store.save") < names.index("api.send_widget")
    assert any(f"#{CMP_ID}" in answer for answer in message.answers)


async def test_widget_api_error_falls_back_to_album_and_reports_id(env):
    env.api.error = WidgetAPIError(400, "INPUT_MEDIA_WIDGET_NOT_ALLOWED")
    message = FakeMessage(env.calls)
    await service.compare_images(message, PNG_A, PNG_B)

    assert len(_saves(env.calls)) == 1
    assert len(message.media_groups) == 1
    media = message.media_groups[0]
    assert [item.media.filename for item in media] == ["before.webp", "after.webp"]
    assert any(f"#{CMP_ID}" in answer for answer in message.answers)


async def test_widget_too_large_reports_text_and_saves_nothing(env, monkeypatch):
    def boom(*args, **kwargs):
        raise build.WidgetTooLarge

    monkeypatch.setattr(build, "build", boom)
    message = FakeMessage(env.calls)
    await service.compare_images(message, PNG_A, PNG_B)

    assert message.answers == [service.TOO_LARGE_TEXT]
    assert not _saves(env.calls)
    assert not _widgets(env.calls)


async def test_not_an_image_reports_text_and_saves_nothing(env):
    message = FakeMessage(env.calls)
    await service.compare_images(message, b"this is not an image", PNG_B)

    assert len(message.answers) == 1
    assert "картинка" in message.answers[0].lower()
    assert not _saves(env.calls)
    assert not _widgets(env.calls)


async def test_non_image_document_is_rejected_while_waiting(env):
    service.start_empty(USER_ID)
    message = FakeMessage(
        env.calls,
        document=SimpleNamespace(file_id="doc-1", file_size=100, mime_type="application/pdf"),
    )
    await service.add_image(message)

    assert len(message.answers) == 1
    assert "не картинка" in message.answers[0].lower()
    # Ожидание не сломано: можно прислать нормальную картинку.
    assert service.is_waiting(USER_ID) is True
