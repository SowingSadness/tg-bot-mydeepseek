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


class FakeBot:
    """Двойник `Message.bot` с двумя нужными методами и возможностью падать."""

    def __init__(self, calls: list, *, send_error=None, delete_error=None) -> None:
        self.calls = calls
        self.send_error = send_error
        self.delete_error = delete_error
        self.sent: list = []

    async def send_photo(self, chat_id: int, photo, **kwargs):
        self.calls.append(("bot.send_photo", chat_id, photo, kwargs))
        if self.send_error is not None:
            raise self.send_error
        sent = SimpleNamespace(
            message_id=900 + len(self.sent),
            photo=[
                SimpleNamespace(file_id="preview-file-small", width=320),
                SimpleNamespace(file_id=f"preview-file-{len(self.sent) + 1}", width=2560),
            ],
        )
        self.sent.append(sent)
        return sent

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls.append(("bot.delete_message", chat_id, message_id))
        if self.delete_error is not None:
            raise self.delete_error


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
        bot: FakeBot | None = None,
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
        self.bot = bot if bot is not None else FakeBot(calls)
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

    async def set_preview_file_id(self, cmp_id: str, file_id: str) -> None:
        self.calls.append(("store.set_preview_file_id", cmp_id, file_id))

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


class FakeOriginals:
    """Двойник `originals.save`: пишет вызов в общий список и умеет «падать»."""

    def __init__(self, calls: list) -> None:
        self.calls = calls
        self.error: Exception | None = None

    def save(
        self,
        cache_dir,
        cmp_id: str,
        img_a,
        img_b,
        *,
        width: int = 1600,
        aspect: str = "3:2",
    ) -> dict[str, str]:
        self.calls.append(("originals.save", cache_dir, cmp_id, width, aspect))
        if self.error is not None:
            raise self.error
        return {}


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
    """Свежие фейки кеша/API, короткий дебаунс и пустой буфер на каждый тест.

    `originals.save` тоже подменён: настоящий кодирует AVIF в диск, а /cmp-тестам
    нужен только сам факт вызова и его аргументы.
    """
    calls: list = []
    store = FakeStore(calls)
    fake_api = FakeAPI(calls)
    fake_originals = FakeOriginals(calls)
    service.configure_store(store)
    api_module.configure(fake_api)
    monkeypatch.setattr(service.originals, "save", fake_originals.save)
    monkeypatch.setattr(service, "_album_wait", lambda: 0.01)
    _drop_albums()

    yield SimpleNamespace(
        calls=calls, store=store, api=fake_api, originals=fake_originals
    )

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


def _by_name(calls: list, name: str) -> list:
    return [call for call in calls if call[0] == name]


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


@pytest.mark.parametrize("query_key", ["startapp", "tgWebAppStartParam"])
async def test_compare_urls_failure_log_masks_startapp_token(
    env, monkeypatch, caplog, query_key
):
    """URL с токеном в логе замаскирован, а ответ и побочные эффекты — как раньше."""
    token = "337326_AbCdEfGhIjKlMnOpQrSt"
    url = f"https://t.me/cmpbot?{query_key}={token}"

    async def fake_download(session, url, *, max_bytes, timeout, allow_private):
        raise RuntimeError(f"connection reset: {url}")

    monkeypatch.setattr(images, "download_url", fake_download)
    message = FakeMessage(env.calls)
    with caplog.at_level(logging.INFO, logger=service.log.name):
        await service.compare_urls(message, [url, "https://b.test/two.jpg"])

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == service.log.name
    ]
    assert len(lines) == 1, caplog.text
    line = lines[0]
    assert token not in line
    assert f"{query_key}=<redacted>" in line
    assert "https://t.me/cmpbot" in line
    assert "\n" not in line
    assert [rec.levelno for rec in caplog.records if rec.name == service.log.name] == [
        logging.INFO
    ]

    assert message.answers == [
        "Не удалось скачать картинку по ссылке, попробуйте ещё раз."
    ]
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
    # Подпись — две строки: заголовок и `@бот #номер` отдельной строкой.
    assert widgets[0][3] == "Номер сравнения:\n@test_bot #482913"
    assert "\n@test_bot" in widgets[0][3]
    assert f"#{CMP_ID}" in widgets[0][3]
    assert widgets[0][1] == CHAT_ID
    assert widgets[0][4] == 0 and widgets[0][5] == 0
    # AR бабла берётся из `Built.aspect`, а не из дефолта `render_cached`.
    assert "aspect-ratio: 3 / 2" in widgets[0][2]

    names = _names(env.calls)
    assert names.index("store.save") < names.index("api.send_widget")
    assert _saves(env.calls)[0][2]["aspect"] == "3:2"
    # Подсказка после успеха — тоже две строки, `@бот #номер` с новой строки.
    assert message.answers == ["Номер сравнения: #482913\n@test_bot #482913"]
    assert "\n@test_bot" in message.answers[0]


async def test_originals_are_saved_after_store_and_before_send(env):
    """Оригиналы для мини-аппа сохраняются после записи в кеш и до отправки виджета."""
    message = FakeMessage(env.calls)
    await service.compare_images(message, PNG_A, PNG_B)

    saves = _by_name(env.calls, "originals.save")
    assert len(saves) == 1
    _, cache_dir, cmp_id, width, aspect = saves[0]
    assert cache_dir == settings.cmp_cache_dir
    assert cmp_id == CMP_ID
    # `CMP_ORIGINALS_WIDTH` появится в конфиге на шаге 7 — пока дефолт 1600.
    assert width == 1600
    assert aspect == "3:2"

    names = _names(env.calls)
    assert (
        names.index("store.save")
        < names.index("originals.save")
        < names.index("api.send_widget")
    )


async def test_originals_failure_warns_and_keeps_widget_hint_and_preview(env, caplog):
    """Сбой сохранения оригиналов: warning, но виджет, подсказка и превью на месте."""
    env.originals.error = RuntimeError("disk full")
    message = FakeMessage(env.calls)

    with caplog.at_level(logging.WARNING, logger=service.log.name):
        await service.compare_images(message, PNG_A, PNG_B)

    assert "оригинал" in caplog.text and "disk full" in caplog.text
    assert f"#{CMP_ID}" in caplog.text
    assert len(_widgets(env.calls)) == 1
    assert message.answers == ["Номер сравнения: #482913\n@test_bot #482913"]
    assert len(_by_name(env.calls, "bot.send_photo")) == 1
    assert _by_name(env.calls, "store.set_preview_file_id")


async def test_widget_api_error_falls_back_to_album_and_reports_id(env, caplog):
    env.api.error = WidgetAPIError(400, "INPUT_MEDIA_WIDGET_NOT_ALLOWED")
    message = FakeMessage(env.calls)
    with caplog.at_level(logging.WARNING, logger=service.log.name):
        await service.compare_images(message, PNG_A, PNG_B)

    assert f"cmp=#{CMP_ID}" in caplog.text
    assert len(_saves(env.calls)) == 1
    assert len(message.media_groups) == 1
    media = message.media_groups[0]
    assert [item.media.filename for item in media] == ["before.webp", "after.webp"]
    assert any(f"#{CMP_ID}" in answer for answer in message.answers)


async def test_widget_too_large_reports_text_and_saves_nothing(env, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise build.WidgetTooLarge

    monkeypatch.setattr(build, "build", boom)
    message = FakeMessage(env.calls)
    with caplog.at_level(logging.WARNING, logger=service.log.name):
        await service.compare_images(message, PNG_A, PNG_B)

    # Пользователю — прежний текст, в лог — WARNING с контекстом и честной пометкой,
    # что номер сравнения ещё не присвоен (кеш пишется только после сборки).
    assert message.answers == [service.TOO_LARGE_TEXT]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, caplog.text
    assert f"chat={CHAT_ID}" in warnings[0]
    assert f"user={USER_ID}" in warnings[0]
    assert "cmp=<не присвоен>" in warnings[0]
    assert "не уложился" in warnings[0]
    assert not _saves(env.calls)
    assert not _widgets(env.calls)


async def test_preview_is_uploaded_deleted_and_file_id_saved_last(env):
    """Успешный путь: композит уходит невидимо, сообщение удаляется, file_id — в кеш."""
    message = FakeMessage(env.calls)
    await service.compare_images(message, PNG_A, PNG_B)

    sends = _by_name(env.calls, "bot.send_photo")
    assert len(sends) == 1
    _, chat_id, upload, kwargs = sends[0]
    assert chat_id == CHAT_ID
    assert upload.filename == "cmp-preview.jpg"
    assert upload.data[:2] == b"\xff\xd8"  # настоящий JPEG-композит
    assert kwargs == {"disable_notification": True}

    deletes = _by_name(env.calls, "bot.delete_message")
    assert deletes == [("bot.delete_message", CHAT_ID, message.bot.sent[0].message_id)]

    names = _names(env.calls)
    assert (
        names.index("api.send_widget")
        < names.index("bot.send_photo")
        < names.index("bot.delete_message")
        < names.index("store.set_preview_file_id")
    )
    # file_id берётся из последнего (самого большого) размера фото и только после удаления.
    assert _by_name(env.calls, "store.set_preview_file_id") == [
        ("store.set_preview_file_id", CMP_ID, "preview-file-1")
    ]
    assert any(f"#{CMP_ID}" in answer for answer in message.answers)


async def test_preview_send_failure_warns_and_keeps_widget_and_hint(env, caplog):
    """send_photo упал: предупреждение в лог, file_id нет — но бабл и подсказка ушли."""
    message = FakeMessage(env.calls)
    message.bot.send_error = RuntimeError("network down")

    with caplog.at_level(logging.WARNING, logger=service.log.name):
        await service.compare_images(message, PNG_A, PNG_B)

    assert "превью" in caplog.text and "network down" in caplog.text
    assert f"#{CMP_ID}" in caplog.text
    assert len(_widgets(env.calls)) == 1
    assert any(f"#{CMP_ID}" in answer for answer in message.answers)
    assert not _by_name(env.calls, "store.set_preview_file_id")
    assert not _by_name(env.calls, "bot.delete_message")


async def test_preview_delete_failure_warns_and_does_not_save_file_id(env, caplog):
    """delete_message упал: сообщение осталось, file_id не кешируем, /cmp не ломается."""
    message = FakeMessage(env.calls)
    message.bot.delete_error = RuntimeError("message to delete not found")

    with caplog.at_level(logging.WARNING, logger=service.log.name):
        await service.compare_images(message, PNG_A, PNG_B)

    assert "превью" in caplog.text and "delete" in caplog.text.lower()
    assert f"#{CMP_ID}" in caplog.text
    assert len(_widgets(env.calls)) == 1
    assert any(f"#{CMP_ID}" in answer for answer in message.answers)
    assert not _by_name(env.calls, "store.set_preview_file_id")


async def test_preview_is_uploaded_after_widget_fallback(env):
    """Фолбэк-альбом вместо виджета не отменяет превью: file_id всё равно кешируется."""
    env.api.error = WidgetAPIError(400, "INPUT_MEDIA_WIDGET_NOT_ALLOWED")
    message = FakeMessage(env.calls)
    await service.compare_images(message, PNG_A, PNG_B)

    assert len(message.media_groups) == 1
    assert len(_by_name(env.calls, "bot.send_photo")) == 1
    assert _by_name(env.calls, "store.set_preview_file_id") == [
        ("store.set_preview_file_id", CMP_ID, "preview-file-1")
    ]


async def test_not_an_image_reports_text_and_saves_nothing(env):
    message = FakeMessage(env.calls)
    await service.compare_images(message, b"this is not an image", PNG_B)

    assert len(message.answers) == 1
    assert "картинка" in message.answers[0].lower()
    assert not _saves(env.calls)
    assert not _widgets(env.calls)


async def test_image_error_warns_with_context_and_keeps_user_text(
    env, monkeypatch, caplog
):
    """`images.ImageError` в сборке: WARNING с chat/user/причиной, ответ прежний."""

    def boom(_data):
        raise images.ImageError("картинка битая")

    monkeypatch.setattr(service.images, "load", boom)
    message = FakeMessage(env.calls)
    with caplog.at_level(logging.WARNING, logger=service.log.name):
        await service.compare_images(message, PNG_A, PNG_B)

    assert message.answers == ["картинка битая"]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, caplog.text
    line = warnings[0]
    assert f"chat={CHAT_ID}" in line
    assert f"user={USER_ID}" in line
    assert "картинка битая" in line
    assert "cmp=<не присвоен>" in line
    assert "\n" not in line
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
