"""Тесты сценариев `/cmp` и роутера — без сети и без реального Telegram.

Сеть подменяется на уровне модулей (`images.download_url`, `respond.download_photo`),
кеш и Bot API — лёгкие фейки через `configure_store` / `api.configure`.
Pillow работает по-настоящему: `images.load` + `build.build` на крошечных PNG.

Главный сценарий — альбом: подпись `/cmp` только у первого фото, у всех общий
`media_group_id`, второе фото приходит отдельным апдейтом. Дебаунс подменён на
0.01 с, поэтому тесты не спят секундами.
"""

from __future__ import annotations

import asyncio
import logging
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
        media_group_id: str | None = None,
        message_id: int = 1,
    ) -> None:
        self.calls = calls
        self.chat = FakeChat()
        self.from_user = FakeUser(user_id)
        self.text = text
        self.caption = caption
        self.photo = photo
        self.document = document
        self.reply_to_message = reply_to_message
        self.media_group_id = media_group_id
        self.message_id = message_id
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


def photo(file_id: str = "photo-1") -> list:
    """Список размеров фото, как у Telegram: берётся последний."""
    return [SimpleNamespace(file_id=f"{file_id}-small", file_size=50), SimpleNamespace(
        file_id=file_id, file_size=100
    )]


def photo_message(
    calls: list,
    file_id: str = "photo-1",
    *,
    caption: str | None = None,
    media_group_id: str | None = None,
    message_id: int = 1,
) -> FakeMessage:
    return FakeMessage(
        calls,
        caption=caption,
        photo=photo(file_id),
        media_group_id=media_group_id,
        message_id=message_id,
    )


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


def _drop_albums() -> None:
    """Снимает буферы, дебаунс-задачи и закрытые ключи между тестами."""
    for album in list(service._albums.values()):
        if album.task is not None:
            album.task.cancel()
    service._albums.clear()
    for task in list(service._closed.values()):
        task.cancel()
    service._closed.clear()


async def settle() -> None:
    """Даёт задачам, созданным через `asyncio.create_task`, выполниться."""
    await asyncio.sleep(0.05)


@pytest.fixture
def env(monkeypatch):
    """Свежие фейки кеша/API, короткий дебаунс и пустой буфер на каждый тест."""
    calls: list = []
    store = FakeStore(calls)
    fake_api = FakeAPI(calls)
    service.configure_store(store)
    api_module.configure(fake_api)
    monkeypatch.setattr(service, "_album_wait", lambda: 0.01)
    _drop_albums()

    yield SimpleNamespace(calls=calls, store=store, api=fake_api)

    _drop_albums()
    service.configure_store(None)
    api_module.configure(None)


def _names(calls: list) -> list[str]:
    return [call[0] for call in calls]


def _saves(calls: list) -> list:
    return [call for call in calls if call[0] == "store.save"]


def _widgets(calls: list) -> list:
    return [call for call in calls if call[0] == "api.send_widget"]


def _answers(calls: list) -> list[str]:
    return [call[1] for call in calls if call[0] in ("answer", "reply")]


# --------------------------------------------------------------------- album


async def test_album_of_two_photos_saves_once_and_clears_buffer(env, monkeypatch):
    """Подпись /cmp у первого фото + второе фото того же альбома → одно сравнение."""
    downloaded: list[str] = []

    async def fake_download_photo(message, file_id):
        downloaded.append(file_id)
        return PNG_A if file_id == "photo-1" else PNG_B

    monkeypatch.setattr("bot.respond.download_photo", fake_download_photo)

    first = photo_message(
        env.calls, "photo-1", caption="/cmp", media_group_id="group-1", message_id=10
    )
    await compare_handlers.on_cmp(first)

    assert service.album_pending(CHAT_ID, "group-1", 10) is True
    assert service.album_pending(CHAT_ID, None, 10) is False  # ключ — по альбому

    second = photo_message(env.calls, "photo-2", media_group_id="group-1", message_id=11)
    assert await compare_handlers.AlbumContinuation()(second) is True
    await compare_handlers.on_album_image(second)

    # Буфер очищается сразу, не дожидаясь дебаунса; ключ закрыт для третьего фото.
    assert service._albums == {}
    assert service.album_pending(CHAT_ID, "group-1", 11) is True
    assert (CHAT_ID, "group-1") in service._closed

    await settle()

    assert downloaded == ["photo-1", "photo-2"]
    saves = _saves(env.calls)
    assert len(saves) == 1
    assert saves[0][2]["aspect"] == "3:2"
    assert saves[0][2]["image_a"].startswith(b"RIFF")
    assert saves[0][2]["image_b"].startswith(b"RIFF")
    assert saves[0][2]["image_a"] != saves[0][2]["image_b"]
    assert len(_widgets(env.calls)) == 1
    # Подсказка с номером — и ни одной просьбы дослать картинку.
    assert any(f"#{CMP_ID}" in answer for answer in _answers(env.calls))
    assert not any(service.NEED_TWO_TEXT in answer for answer in _answers(env.calls))


async def test_album_key_is_reserved_before_first_photo_downloads(env, monkeypatch):
    """Гонка aiogram: второе фото приходит, пока первая картинка ещё скачивается.

    `handle_as_tasks=True` обрабатывает апдейты альбома конкурентно, поэтому
    `arm_album` обязан занять ключ **до** `await download_message_image`.
    Первое скачивание здесь висит на `asyncio.Event`: если бы ключ появился
    после скачивания, второе фото не узнало бы альбом и ушло в
    `private.on_photo`/DeepSeek. Проверяем: ключ виден сразу, второе фото
    попадает в тот же буфер, в кеш и виджет уходит ровно одно сравнение.
    """
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    downloaded: list[str] = []

    async def fake_download_photo(message, file_id):
        downloaded.append(file_id)
        if file_id == "photo-1":
            first_started.set()
            await release_first.wait()
            return PNG_A
        return PNG_B

    monkeypatch.setattr("bot.respond.download_photo", fake_download_photo)

    first = photo_message(
        env.calls, "photo-1", caption="/cmp", media_group_id="group-race", message_id=10
    )
    task = asyncio.create_task(compare_handlers.on_cmp(first))
    await asyncio.wait_for(first_started.wait(), 1)

    # Первая картинка ещё висит, но ключ уже зарезервирован под этот альбом.
    assert service.album_pending(CHAT_ID, "group-race", 10) is True
    assert service._albums != {}

    second = photo_message(
        env.calls, "photo-2", media_group_id="group-race", message_id=11
    )
    assert await compare_handlers.AlbumContinuation()(second) is True
    await compare_handlers.on_album_image(second)

    release_first.set()
    await asyncio.wait_for(task, 1)
    await settle()

    assert downloaded == ["photo-1", "photo-2"]
    assert len(_saves(env.calls)) == 1
    assert len(_widgets(env.calls)) == 1
    assert service._albums == {}
    # Никаких «нужны две картинки»: оба фото попали в один альбом, а не в DeepSeek.
    assert not any(
        service.NEED_TWO_TEXT in answer
        for answer in first.answers + second.answers
    )


async def test_third_album_photo_is_dropped_not_sent_to_deepseek(
    env, monkeypatch, caplog
):
    """Третье фото того же альбома: ключ закрыт, сервис молчит и ничего не скачивает."""
    downloaded: list[str] = []

    async def fake_download_photo(message, file_id):
        downloaded.append(file_id)
        return PNG_A if file_id == "photo-1" else PNG_B

    monkeypatch.setattr("bot.respond.download_photo", fake_download_photo)
    monkeypatch.setattr(service, "_close_wait", lambda: 0.2)

    first = photo_message(
        env.calls, "photo-1", caption="/cmp", media_group_id="group-3", message_id=10
    )
    await compare_handlers.on_cmp(first)
    second = photo_message(
        env.calls, "photo-2", media_group_id="group-3", message_id=11
    )
    await compare_handlers.on_album_image(second)
    await settle()

    assert len(_saves(env.calls)) == 1
    assert len(_widgets(env.calls)) == 1

    # Ключ закрыт (таймер ещё не истёк), поэтому фильтр пропускает третье фото...
    assert service.album_pending(CHAT_ID, "group-3", 12) is True
    third = photo_message(
        env.calls, "photo-3", media_group_id="group-3", message_id=12
    )
    assert await compare_handlers.AlbumContinuation()(third) is True
    with caplog.at_level(logging.DEBUG, logger=service.log.name):
        await compare_handlers.on_album_image(third)
    await settle()

    # ...а сервис молча дропает: ни скачивания, ни ответа, ни второго сравнения.
    assert downloaded == ["photo-1", "photo-2"]
    assert third.answers == []
    assert len(_saves(env.calls)) == 1
    assert len(_widgets(env.calls)) == 1
    assert "игнориру" in caplog.text

    # Таймер закрытого ключа снимает его, и обычные фото снова не наши.
    await asyncio.sleep(0.25)
    assert service.album_pending(CHAT_ID, "group-3", 12) is False
    assert service._closed == {}


async def test_album_with_one_photo_answers_instruction_and_keeps_no_state(env, monkeypatch):
    """Дебаунс истёк, а картинка одна → инструкция, никаких записей и состояния."""

    async def fake_download_photo(message, file_id):
        return PNG_A

    monkeypatch.setattr("bot.respond.download_photo", fake_download_photo)

    message = photo_message(
        env.calls, "photo-1", caption="/cmp", media_group_id="group-1", message_id=10
    )
    await compare_handlers.on_cmp(message)
    assert service.album_pending(CHAT_ID, "group-1", 10) is True

    await settle()

    assert service._albums == {}
    assert service.album_pending(CHAT_ID, "group-1", 10) is False
    assert message.answers == [service.NEED_TWO_TEXT]
    assert not _saves(env.calls)
    assert not _widgets(env.calls)


async def test_two_separate_photos_without_media_group_do_not_chain(env, monkeypatch):
    """Два одиночных фото без альбома не склеиваются: второе — не продолжение."""

    async def fake_download_photo(message, file_id):
        return PNG_A

    monkeypatch.setattr("bot.respond.download_photo", fake_download_photo)

    first = photo_message(env.calls, "photo-1", caption="/cmp", message_id=10)
    await compare_handlers.on_cmp(first)
    await settle()
    assert first.answers == [service.NEED_TWO_TEXT]

    second = photo_message(env.calls, "photo-2", message_id=11)
    assert service.album_pending(CHAT_ID, None, 11) is False
    assert await compare_handlers.AlbumContinuation()(second) is False
    # Даже при прямом вызове хендлер ничего не делает: буфера нет.
    await compare_handlers.on_album_image(second)
    await settle()

    assert second.answers == []
    assert not _saves(env.calls)
    assert not _widgets(env.calls)


async def test_flush_with_three_images_compares_first_two_and_logs(env, caplog):
    """Если в буфере оказалось 3+ картинки — сравниваем первые две, в лог — заметка."""
    message = FakeMessage(env.calls)
    service._albums[(CHAT_ID, "group-3")] = service.Album(
        chat_id=CHAT_ID,
        message=message,
        images=[PNG_A, PNG_B, png((10, 200, 10))],
    )

    with caplog.at_level(logging.INFO, logger=service.log.name):
        service._flush((CHAT_ID, "group-3"))
        await settle()

    assert service._albums == {}
    assert len(_saves(env.calls)) == 1
    assert "первые две" in caplog.text


# ----------------------------------------------------------------- URL/reply


async def test_compare_urls_downloads_both_with_settings_and_saves_aspect(env, monkeypatch):
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
    assert _saves(env.calls)[0][2]["aspect"] == "3:2"
    assert len(_widgets(env.calls)) == 1
    assert service._albums == {}


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

    assert downloaded == ["reply-photo"]
    assert urls == ["https://b.test/two.jpg"]
    assert len(_saves(env.calls)) == 1
    saved = _saves(env.calls)[0][2]
    assert saved["image_a"].startswith(b"RIFF")
    assert saved["image_b"].startswith(b"RIFF")
    assert saved["aspect"] == "3:2"
    assert len(_widgets(env.calls)) == 1
    assert service._albums == {}


async def test_cmp_without_arguments_asks_for_two_pictures(env):
    message = FakeMessage(env.calls, text="/cmp")
    await compare_handlers.on_cmp(message)

    assert message.answers == [service.NEED_TWO_TEXT]
    assert service._albums == {}
    assert not _saves(env.calls)
    assert not _widgets(env.calls)


async def test_cmp_with_single_url_asks_for_two_pictures(env, monkeypatch):
    called = False

    async def fake_download_url(session, url, *, max_bytes, timeout, allow_private):
        nonlocal called
        called = True
        return PNG_A

    monkeypatch.setattr(images, "download_url", fake_download_url)
    message = FakeMessage(env.calls, text="/cmp https://a.test/one.jpg")
    await compare_handlers.on_cmp(message)

    assert called is False
    assert message.answers == [service.NEED_TWO_TEXT]
    assert service._albums == {}
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


async def test_non_image_document_with_cmp_caption_reports_and_keeps_no_state(env):
    message = FakeMessage(
        env.calls,
        caption="/cmp",
        document=SimpleNamespace(
            file_id="doc-1", file_size=100, mime_type="application/pdf"
        ),
    )
    await compare_handlers.on_cmp(message)

    assert len(message.answers) == 1
    assert "не картинка" in message.answers[0].lower()
    assert service._albums == {}
    assert not _saves(env.calls)


# -------------------------------------------------------------- caption/cache


async def test_caption_contains_id_and_save_goes_before_send(env):
    message = FakeMessage(env.calls)
    await service.compare_images(message, PNG_A, PNG_B)

    widgets = _widgets(env.calls)
    assert len(widgets) == 1
    assert f"#{CMP_ID}" in widgets[0][3]
    assert widgets[0][1] == CHAT_ID
    assert widgets[0][4] == 0 and widgets[0][5] == 0
    # AR бабла берётся из `Built.aspect`, а не из дефолта `render_cached`.
    assert "aspect-ratio: 3 / 2" in widgets[0][2]

    names = _names(env.calls)
    assert names.index("store.save") < names.index("api.send_widget")
    assert _saves(env.calls)[0][2]["aspect"] == "3:2"
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


# ------------------------------------------------------------ old API removed


def test_sequential_mode_is_gone():
    """Последовательного режима больше нет: ни в сервисе, ни в хендлерах.

    Имена собраны склейкой литералов, чтобы этот же набор не находился поиском
    по исходникам как уцелевший старый API.
    """
    for name in (
        "Pending",
        "_" "pending",
        "is_" "waiting",
        "start_" "empty",
        "start_" "with",
        "add_" "image",
        "FIRST_" "IMAGE_TEXT",
        "EXPIRED_" "TEXT",
    ):
        assert not hasattr(service, name), name

    for name in ("Awaiting" "Images", "on_" "image"):
        assert not hasattr(compare_handlers, name), name
    assert "Awaiting" "Images" not in compare_handlers.__all__
    assert not hasattr(settings, "cmp_" "pending_ttl")


# ------------------------------------------------------- /cmp #номер из кеша


class CachedStore:
    """Кеш-двойник для повторной отправки: отдаёт готовое сравнение или ничего."""

    def __init__(self, calls: list, item=None) -> None:
        self.calls = calls
        self.item = item

    async def get(self, cmp_id: str, *, owner_id: int | None = None):
        self.calls.append(("store.get", cmp_id, owner_id))
        return self.item


def _cached(cmp_id: str = CMP_ID, owner_id: int = USER_ID):
    from bot.compare.store import Cached

    return Cached(
        id=cmp_id,
        owner_id=owner_id,
        created_at=0.0,
        width=430,
        quality=34,
        labels=("До", "После"),
        zlib_bytes=1024,
        image_a=b"RIFF-a-webp-bytes",
        image_b=b"RIFF-b-webp-bytes",
    )


async def test_cmp_with_number_resends_widget_from_cache(env):
    """/cmp #482913 в личке — бабл из кеша без скачиваний и без DeepSeek."""
    env.calls.clear()
    store = CachedStore(env.calls, _cached())
    service.configure_store(store)
    message = FakeMessage(env.calls, text=f"/cmp #{CMP_ID}")

    await compare_handlers.on_cmp(message)

    assert ("store.get", CMP_ID, USER_ID) in env.calls
    widgets = _widgets(env.calls)
    assert len(widgets) == 1
    assert widgets[0][1] == CHAT_ID
    assert f"#{CMP_ID}" in widgets[0][3]
    assert not message.media_groups


async def test_cmp_with_unknown_number_reports_and_sends_nothing(env):
    env.calls.clear()
    service.configure_store(CachedStore(env.calls, None))
    message = FakeMessage(env.calls, text=f"/cmp #{CMP_ID}")

    await compare_handlers.on_cmp(message)

    assert any(f"#{CMP_ID}" in answer and "не найдено" in answer for answer in message.answers)
    assert not _widgets(env.calls)
    assert not message.media_groups


async def test_cmp_with_number_falls_back_to_album_when_widget_refused(env):
    """/cmp #номер — тот же фолбэк, что и при первом сравнении."""
    env.calls.clear()
    api_module.configure(FakeAPI(env.calls, WidgetAPIError(400, "INPUT_MEDIA_WIDGET_NOT_ALLOWED")))
    service.configure_store(CachedStore(env.calls, _cached()))
    message = FakeMessage(env.calls, text=f"/cmp #{CMP_ID}")

    await compare_handlers.on_cmp(message)

    assert len(message.media_groups) == 1
    assert any(f"#{CMP_ID}" in answer for answer in message.answers)


# ------------------------------------------------------------ группы


async def test_cmp_works_in_group_with_command_mention(env, monkeypatch):
    """`/cmp@bot` из группы: та же сборка, виджет уходит в чат группы."""
    async def fake_download(session, url, *, max_bytes, timeout, allow_private):
        return PNG_A if "one" in url else PNG_B

    monkeypatch.setattr(images, "download_url", fake_download)

    group = FakeMessage(
        env.calls,
        text="/cmp@sowing_tg_bot https://a.test/one.jpg https://b.test/two.jpg",
    )
    group.chat = SimpleNamespace(id=-100500, type="supergroup")
    await compare_handlers.on_cmp(group)

    widgets = _widgets(env.calls)
    assert len(widgets) == 1
    assert widgets[0][1] == -100500  # чат группы, не личка
    assert len(_saves(env.calls)) == 1


async def test_group_chats_are_allowed_channel_is_not():
    assert {"private", "group", "supergroup"} <= set(compare_handlers.CMP_CHATS)
    assert "channel" not in compare_handlers.CMP_CHATS


def test_need_two_text_mentions_privacy_only_in_groups():
    private = FakeMessage([])
    group = FakeMessage([])
    group.chat = SimpleNamespace(id=-1, type="supergroup")

    assert service._need_two_text(private) == service.NEED_TWO_TEXT
    assert "privacy mode" in service._need_two_text(group)


async def test_cmp_number_resend_works_in_group(env):
    """`/cmp #номер` в группе — бабл в групповой чат из кеша."""
    env.calls.clear()
    service.configure_store(CachedStore(env.calls, _cached()))
    message = FakeMessage(env.calls, text=f"/cmp@sowing_tg_bot #{CMP_ID}")
    message.chat = SimpleNamespace(id=-100500, type="supergroup")

    await compare_handlers.on_cmp(message)

    widgets = _widgets(env.calls)
    assert len(widgets) == 1
    assert widgets[0][1] == -100500
