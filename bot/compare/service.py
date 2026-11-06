"""Сценарии `/cmp`: две ссылки, альбом из двух картинок или картинка в ответе.

Модуль связывает четыре части: скачивание картинок (`images`), сборку бабла
(`build`), дисковый кеш (`store`) и отправку виджета (`api`). Всё, что видит
пользователь, — короткие русские сообщения; стектрейсы остаются в логе.

Картинки приходят **вместе с командой**: Telegram шлёт каждое фото альбома
отдельным апдейтом с общим `media_group_id`, а подпись обычно есть только у
первого. Поэтому подпись `/cmp` заводит буфер `(chat_id, media_group_id или
message_id)` и дебаунс: следующие картинки того же альбома добавляются в буфер,
а как только их стало две — сравнение запускается сразу, не дожидаясь таймера.

Ключ буфера занимается **синхронно**, ещё до `await` за скачиванием — aiogram
обрабатывает апдейты альбома конкурентно (`handle_as_tasks`), и второе фото не
должно уйти в DeepSeek, пока докачивается первое. После сравнения ключ ещё
недолго живёт в `_closed`: третье фото того же альбома молча дропается.

Порядок в `compare_images` принципиален: **сначала** номер и картинки попадают
в кеш, и только потом уходит `sendWidget`. Иначе inline по `#номер` мог бы
обогнать запись, а фолбэк-альбом остался бы без номера.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aiogram.types import BufferedInputFile, InputMediaDocument

from .. import respond
from ..config import settings
from . import api as api_module
from . import build, images, originals, preview
from .store import ComparisonStore

log = logging.getLogger(__name__)

#: Что показываем, если двух картинок так и не набралось.
NEED_TWO_TEXT = (
    "Нужны две картинки: прикрепите оба фото одним альбомом и подпишите /cmp, "
    "или пришлите ссылки: /cmp https://a.jpg https://b.jpg"
)
#: Не удалось уложить картинки в лимиты виджета.
TOO_LARGE_TEXT = (
    "Картинки слишком пёстрые, чтобы уместиться в бабл — пришлите картинки поменьше."
)

#: Ключ буфера альбома: `(chat_id, media_group_id)` или `(chat_id, message_id)`.
AlbumKey = tuple[int, str | int]


@dataclass
class Album:
    """Собираемый альбом: картинки, последнее сообщение, дебаунс и счётчик скачиваний.

    ``task`` — и есть дебаунс (`expires`): таймер, продлеваемый каждым новым фото.
    ``pending`` — сколько картинок сейчас скачивается; пока счётчик больше нуля,
    дебаунс не сравнивает альбом, иначе медленная первая картинка потеряется.
    ``first_pending`` — в альбоме зарезервирован слот 0 под картинку с подписью
    `/cmp`: она может докачаться позже второго фото, но остаётся первой.
    """

    chat_id: int
    message: object
    images: list[bytes] = field(default_factory=list)
    task: asyncio.Task | None = None
    pending: int = 0
    first_pending: bool = False


_albums: dict[AlbumKey, Album] = {}
#: Только что сравнённые ключи: третье фото альбома молча дропается, а не уходит в DeepSeek.
_closed: dict[AlbumKey, asyncio.Task] = {}

_store: ComparisonStore | None = None


def configure_store(store: ComparisonStore | None) -> None:
    """Запоминает кеш для сценариев (вызывается на старте приложения)."""
    global _store
    _store = store


def get_store() -> ComparisonStore:
    """Возвращает настроенный кеш; без `configure_store` — понятный `RuntimeError`."""
    if _store is None:
        raise RuntimeError(
            "Кеш сравнений не настроен: вызовите bot.compare.service.configure_store(store) "
            "при старте приложения."
        )
    return _store


def _album_wait() -> float:
    """Сколько ждать остальные картинки альбома (тесты подменяют функцию)."""
    return float(settings.cmp_album_wait)


def _preview_width() -> int:
    """Ширина композита-превью: `CMP_PREVIEW_WIDTH`, по умолчанию 2560."""
    try:
        width = int(getattr(settings, "cmp_preview_width", 2560))
    except (TypeError, ValueError):
        return 2560
    return width if width >= 2 else 2560


def _originals_width() -> int:
    """Ширина оригиналов для мини-аппа: `CMP_ORIGINALS_WIDTH`, по умолчанию 1600."""
    default = originals.DEFAULT_WIDTH
    try:
        width = int(getattr(settings, "cmp_originals_width", default))
    except (TypeError, ValueError):
        return default
    return width if width >= 1 else default


def _album_key(message) -> AlbumKey:
    """Альбом опознаётся по `media_group_id`, одиночное сообщение — по `message_id`."""
    group = getattr(message, "media_group_id", None)
    return (message.chat.id, group or message.message_id)


def album_pending(
    chat_id: int,
    media_group_id: str | None = None,
    message_id: int | None = None,
) -> bool:
    """Наш ли ключ: идёт сборка альбома или совсем недавно прошло сравнение."""
    key = (chat_id, media_group_id or message_id)
    return key in _albums or key in _closed


def _current_task() -> asyncio.Task | None:
    """Текущая задача или None вне работающего цикла (синхронные тесты/вызовы)."""
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _schedule(key: AlbumKey, album: Album) -> None:
    """Перезапускает дебаунс: каждое новое фото продлевает ожидание."""
    if album.task is not None:
        album.task.cancel()
    album.task = asyncio.create_task(_expire(key))


async def _expire(key: AlbumKey) -> None:
    """Дебаунс истёк: отдаём буфер на сборку или просим дослать вторую картинку."""
    try:
        await asyncio.sleep(_album_wait())
    except asyncio.CancelledError:
        return
    _flush(key)


def _close_wait() -> float:
    """Сколько держать ключ закрытым после сравнения (тесты подменяют функцию)."""
    return max(_album_wait() * 3.0, 5.0)


def _close(key: AlbumKey) -> None:
    """Помечает ключ закрытым на короткое время: лишние фото альбома уже не нужны."""
    task = _closed.pop(key, None)
    if task is not None:
        task.cancel()
    _closed[key] = asyncio.create_task(_expire_closed(key))


async def _expire_closed(key: AlbumKey) -> None:
    """Снимает закрытый ключ, когда альбом заведомо дослан (или уже не дослан)."""
    try:
        await asyncio.sleep(_close_wait())
    except asyncio.CancelledError:
        return
    if _closed.get(key) is _current_task():
        _closed.pop(key, None)


def _drop_album(key: AlbumKey, album: Album) -> None:
    """Снимает резерв ключа, если картинка так и не скачалась."""
    if _albums.get(key) is album:
        _albums.pop(key, None)
    task, album.task = album.task, None
    if task is not None and task is not _current_task():
        task.cancel()


def _flush(key: AlbumKey) -> None:
    """Забирает буфер: две картинки — сравнение, одна — инструкция. Буфер пуст."""
    album = _albums.get(key)
    if album is None:
        return
    if album.pending > 0:
        # Картинка ещё скачивается: ключ остаётся занятым, дебаунс ждёт дальше.
        if album.task is _current_task():
            album.task = None
        _schedule(key, album)
        return

    _albums.pop(key, None)
    task, album.task = album.task, None
    if task is not None and task is not _current_task():
        task.cancel()

    if len(album.images) >= 2:
        if len(album.images) > 2:
            log.info(
                "Альбом %s: пришло %d картинок — сравниваю первые две",
                key,
                len(album.images),
            )
        _close(key)
        asyncio.create_task(
            compare_images(album.message, album.images[0], album.images[1])
        )
        return

    asyncio.create_task(album.message.answer(NEED_TWO_TEXT))


def _after_append(key: AlbumKey, album: Album) -> None:
    """Две картинки — сразу сравниваем; одна — ждём остальные до дебаунса."""
    if len(album.images) >= 2:
        _flush(key)
    else:
        _schedule(key, album)


def _image_ref(message) -> tuple[str | None, int | None]:
    """Достаёт `(file_id, заявленный размер)` из фото/документа сообщения."""
    photos = getattr(message, "photo", None)
    if photos:
        photo = photos[-1]
        return photo.file_id, getattr(photo, "file_size", None)

    document = getattr(message, "document", None)
    if document is not None:
        mime_type = getattr(document, "mime_type", None) or ""
        if not mime_type.startswith("image/"):
            return None, getattr(document, "file_size", None)
        return document.file_id, getattr(document, "file_size", None)
    return None, None


def _too_big_text() -> str:
    limit_mib = respond.MAX_DOWNLOAD_BYTES // (1024 * 1024)
    return f"Файл больше {limit_mib} МБ — Telegram не даст мне его скачать."


async def download_message_image(message) -> bytes:
    """Скачивает картинку из фото/документа сообщения, проверяя размер.

    Любая проблема — `images.ImageError` с текстом для пользователя.
    """
    file_id, declared_size = _image_ref(message)
    if file_id is None:
        raise images.ImageError("Это не картинка: пришлите фото или файл-изображение.")
    if declared_size and declared_size > respond.MAX_DOWNLOAD_BYTES:
        raise images.ImageError(_too_big_text())

    try:
        raw = await respond.download_photo(message, file_id)
    except Exception as exc:  # noqa: BLE001 — пользователю нужен текст, не стек
        log.info("Не удалось скачать картинку из сообщения: %s", exc)
        raise images.ImageError("Не удалось скачать картинку, попробуйте ещё раз.") from exc

    if len(raw) > respond.MAX_DOWNLOAD_BYTES:
        raise images.ImageError(_too_big_text())
    if not raw:
        raise images.ImageError("Пустой файл — это не картинка.")
    return raw


async def arm_album(message) -> None:
    """Старт альбома по подписи `/cmp` у картинки: первая картинка уже есть.

    Ключ занимается **синхронно**, до первого `await`: aiogram обрабатывает
    апдейты альбома конкурентно, и второе фото может прийти раньше, чем
    докачается первое. Иначе продолжение не узнаёт альбом и уйдёт в DeepSeek.
    """
    key = _album_key(message)
    album = _albums.get(key)
    if album is None:
        album = Album(chat_id=message.chat.id, message=message)
        _albums[key] = album
    album.message = message
    album.pending += 1
    album.first_pending = True
    _schedule(key, album)

    try:
        raw = await download_message_image(message)
    except images.ImageError as exc:
        album.pending -= 1
        album.first_pending = False
        if not album.images and album.pending == 0:
            _drop_album(key, album)
        await message.answer(str(exc))
        return
    album.pending -= 1

    if album.first_pending:
        # Подписанная картинка остаётся первой, даже если докачалась позже.
        album.images.insert(0, raw)
        album.first_pending = False
    else:
        album.images.append(raw)
    _after_append(key, album)


async def add_album_image(message) -> None:
    """Продолжение начатого альбома: добавляет картинку и продлевает дебаунс."""
    key = _album_key(message)
    album = _albums.get(key)
    if album is None:
        if key in _closed:
            # Третье и следующие фото альбома: сравнение уже прошло — молча дропаем.
            log.debug("Альбом %s уже сравнён — фото %s игнорирую", key, message.message_id)
        return

    album.pending += 1
    try:
        raw = await download_message_image(message)
    except images.ImageError as exc:
        await message.answer(str(exc))
        return
    finally:
        album.pending -= 1

    album.message = message
    album.images.append(raw)
    _after_append(key, album)


async def compare_images(message, a: bytes, b: bytes) -> None:
    """Собирает бабл, кладёт сжатые WebP в кеш и отправляет виджет с номером."""
    labels = settings.cmp_labels_pair

    try:
        img_a = images.load(a)
        img_b = images.load(b)
        built = build.build(
            img_a,
            img_b,
            start_width=settings.cmp_target_width,
            min_width=settings.cmp_min_width,
            start_quality=settings.cmp_start_quality,
            min_quality=settings.cmp_min_quality,
            margin=settings.cmp_zlib_margin,
            labels=labels,
            title=settings.cmp_title,
            hint=settings.cmp_hint,
            aspect=settings.cmp_aspect_pair,
        )
    except images.ImageError as exc:
        await message.answer(str(exc))
        return
    except build.WidgetTooLarge:
        await message.answer(TOO_LARGE_TEXT)
        return

    # Кеш — до отправки: номер должен существовать, даже если клиент не примет виджет.
    store = get_store()
    cmp_id = await store.save(
        message.from_user.id,
        image_a=built.image_a,
        image_b=built.image_b,
        width=built.width,
        quality=built.quality,
        labels=labels,
        zlib_bytes=built.zlib_bytes,
        aspect=built.aspect,
    )
    # Оригиналы для мини-аппа: WebP + AVIF в полном разрешении. Украшение и
    # тяжёлое кодирование (to_thread), поэтому любой сбой — только warning.
    try:
        await asyncio.to_thread(
            originals.save,
            settings.cmp_cache_dir,
            cmp_id,
            img_a,
            img_b,
            width=_originals_width(),
            aspect=built.aspect,
        )
    except Exception as exc:  # noqa: BLE001 — оригиналы не должны ломать /cmp
        log.warning("Не удалось сохранить оригиналы сравнения #%s: %s", cmp_id, exc)

    caption = settings.cmp_caption.format(
        id=cmp_id, a=labels[0], b=labels[1], bot=settings.bot_username
    )
    html = build.render_cached(
        image_a=built.image_a,
        image_b=built.image_b,
        labels=labels,
        title=settings.cmp_title,
        hint=settings.cmp_hint,
        aspect=built.aspect,
        width=built.width,
    )

    api = api_module.get_api()
    try:
        await api.send_widget(message.chat.id, html, caption=caption, width=0, height=0)
    except api_module.WidgetAPIError as exc:
        log.warning(
            "sendWidget не принят (user=%s, cmp=#%s): код=%s, описание=%s",
            message.from_user.id,
            cmp_id,
            exc.code,
            exc.description,
        )
        await _send_fallback(message, caption, built)
    else:
        if settings.cmp_show_hint:
            await message.answer(
                f"Номер сравнения: #{cmp_id}\n@{settings.bot_username} #{cmp_id}"
            )

    # Превью — украшение inline-выдачи: его сбой не должен съедать номер и бабл.
    await _upload_preview(message, cmp_id, img_a, img_b, built, store=store)


async def _upload_preview(
    message,
    cmp_id: str,
    img_a,
    img_b,
    built: build.Built,
    *,
    store: ComparisonStore,
) -> None:
    """Загружает композит в Telegram, сразу удаляет сообщение и кеширует `file_id`.

    Композит собирается из тех же `img_a`/`img_b` и того же окна кропа, что и слои
    бабла (`built.aspect`), поэтому превью совпадает со шторкой по участку сцены.
    Отправка невидимая (`disable_notification`), а сообщение удаляется сразу:
    в личке оно нужно только ради `file_id`.

    Рендер PIL (сотни миллисекунд) уводится в поток через `asyncio.to_thread`,
    чтобы не задерживать event loop; исключение из потока приходит в тот же
    `try`, поэтому любая ошибка — `log.warning` и возврат: номер и подсказка
    уже отправлены.
    """
    try:
        jpeg = await asyncio.to_thread(
            preview.render,
            img_a,
            img_b,
            width=_preview_width(),
            aspect=built.aspect,
            labels=settings.cmp_labels_pair,
        )
        sent = await message.bot.send_photo(
            message.chat.id,
            BufferedInputFile(jpeg, filename="cmp-preview.jpg"),
            disable_notification=True,
        )
        await message.bot.delete_message(message.chat.id, sent.message_id)
        await store.set_preview_file_id(cmp_id, sent.photo[-1].file_id)
    except Exception as exc:  # noqa: BLE001 — превью не должно ломать /cmp
        log.warning("Не удалось загрузить превью сравнения #%s: %s", cmp_id, exc)


async def _send_fallback(message, caption: str, built: build.Built) -> None:
    """Клиент не принял виджет: отдаём те же WebP альбомом и номер в тексте."""
    media = [
        InputMediaDocument(
            media=BufferedInputFile(built.image_a, filename="before.webp")
        ),
        InputMediaDocument(
            media=BufferedInputFile(built.image_b, filename="after.webp")
        ),
    ]
    try:
        await message.answer_media_group(media)
    except Exception:  # noqa: BLE001 — номер всё равно должен дойти текстом
        log.exception("Не удалось отправить альбом с картинками сравнения")

    await message.answer(
        f"{caption}\n\n⚠️ Клиент не принял виджет — отправляю обе картинки альбомом."
    )


async def compare_urls(message, urls: list[str]) -> None:
    """Скачивает обе ссылки (с теми же лимитами, что и для одиночной) и сравнивает."""
    api = api_module.get_api()
    blobs: list[bytes] = []
    for url in urls:
        try:
            blobs.append(
                await images.download_url(
                    api.session,
                    url,
                    max_bytes=settings.cmp_max_bytes,
                    timeout=15.0,
                    allow_private=settings.cmp_allow_private_hosts,
                )
            )
        except images.ImageError as exc:
            await message.answer(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — сеть бывает разной, стек не нужен
            log.info("Не удалось скачать картинку по ссылке %s: %s", url, exc)
            await message.answer("Не удалось скачать картинку по ссылке, попробуйте ещё раз.")
            return

    if len(blobs) < 2:
        await message.answer("Нужны две ссылки на картинки.")
        return

    await compare_images(message, blobs[0], blobs[1])


__all__ = [
    "Album",
    "NEED_TWO_TEXT",
    "TOO_LARGE_TEXT",
    "add_album_image",
    "album_pending",
    "arm_album",
    "compare_images",
    "compare_urls",
    "configure_store",
    "download_message_image",
    "get_store",
]
