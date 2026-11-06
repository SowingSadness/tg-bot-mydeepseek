"""Сценарии `/cmp`: две ссылки, две присланные картинки или картинка в ответе.

Модуль связывает четыре части: скачивание картинок (`images`), сборку бабла
(`build`), дисковый кеш (`store`) и отправку виджета (`api`). Всё, что видит
пользователь, — короткие русские сообщения; стектрейсы остаются в логе.

Порядок в `compare_images` принципиален: **сначала** номер и картинки попадают
в кеш, и только потом уходит `sendWidget`. Иначе inline по `#номер` мог бы
обогнать запись, а фолбэк-альбом остался бы без номера.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from aiogram.types import BufferedInputFile, InputMediaDocument

from .. import respond
from ..config import settings
from . import api as api_module
from . import build, images
from .store import ComparisonStore

log = logging.getLogger(__name__)

#: Что показываем, если сравнивать пока нечего.
START_INSTRUCTION = (
    "Пришлите две картинки: двумя сообщениями, альбомом или ссылками: "
    "/cmp https://a.jpg https://b.jpg"
)
#: Первая картинка принята.
FIRST_IMAGE_TEXT = "1/2 — пришлите вторую картинку."
#: Ожидание второй картинки протухло.
EXPIRED_TEXT = "Ожидание второй картинки истекло — начните заново: /cmp"
#: Не удалось уложить картинки в лимиты виджета.
TOO_LARGE_TEXT = (
    "Картинки слишком пёстрые, чтобы уместиться в бабл — пришлите картинки поменьше."
)


@dataclass
class Pending:
    """Ожидание второй картинки: первая (если есть) и дедлайн по `monotonic`."""

    first: bytes | None
    expires: float


_pending: dict[int, Pending] = {}

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


def _now() -> float:
    """Монотонные часы ожидания (тесты подменяют функцию, а не модуль `time`)."""
    return time.monotonic()


def _state(user_id: int) -> tuple[Pending | None, bool]:
    """Текущее ожидание и признак «было, но истекло» (истёкшее сразу удаляется)."""
    state = _pending.get(user_id)
    if state is None:
        return None, False
    if state.expires <= _now():
        _pending.pop(user_id, None)
        return None, True
    return state, False


def is_waiting(user_id: int) -> bool:
    """Ждём ли от пользователя вторую картинку (с учётом TTL)."""
    state, _ = _state(user_id)
    return state is not None


def start_empty(user_id: int) -> None:
    """Начинает ожидание с нуля: первая картинка ещё не пришла."""
    _pending[user_id] = Pending(None, _now() + settings.cmp_pending_ttl)


def clear(user_id: int) -> None:
    """Сбрасывает ожидание пользователя."""
    _pending.pop(user_id, None)


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


async def start_with(message, first: bytes | None) -> None:
    """Первая картинка уже есть (или её нет) — ждём вторую."""
    user_id = message.from_user.id
    if first is None:
        start_empty(user_id)
        await message.answer(START_INSTRUCTION)
        return

    _pending[user_id] = Pending(first, _now() + settings.cmp_pending_ttl)
    await message.answer(FIRST_IMAGE_TEXT)


async def add_image(message) -> None:
    """Принимает очередную картинку пользователя: первая → 1/2, вторая → сравнение."""
    user_id = message.from_user.id
    state, expired = _state(user_id)
    if state is None:
        # Не ждём: чужой сценарий (например, фото для DeepSeek). Но если ожидание
        # только что истекло — подсказываем начать заново, а не молчим.
        if expired:
            await message.answer(EXPIRED_TEXT)
        return

    try:
        raw = await download_message_image(message)
    except images.ImageError as exc:
        await message.answer(str(exc))
        return

    if state.first is None:
        state.first = raw
        state.expires = _now() + settings.cmp_pending_ttl
        await message.answer(FIRST_IMAGE_TEXT)
        return

    first = state.first
    clear(user_id)
    await compare_images(message, first, raw)


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
    )
    caption = settings.cmp_caption.format(id=cmp_id, a=labels[0], b=labels[1])
    html = build.render_cached(
        image_a=built.image_a,
        image_b=built.image_b,
        labels=labels,
        title=settings.cmp_title,
        hint=settings.cmp_hint,
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
        return

    if settings.cmp_show_hint:
        await message.answer(
            f"Номер сравнения: #{cmp_id}. В любом чате: @{settings.bot_username} #{cmp_id}"
        )


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
    "EXPIRED_TEXT",
    "FIRST_IMAGE_TEXT",
    "Pending",
    "START_INSTRUCTION",
    "TOO_LARGE_TEXT",
    "add_image",
    "clear",
    "compare_images",
    "compare_urls",
    "configure_store",
    "download_message_image",
    "get_store",
    "is_waiting",
    "start_empty",
    "start_with",
]
