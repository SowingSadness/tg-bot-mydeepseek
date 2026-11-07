"""Хендлеры `/cmp`: ссылки, альбом из двух картинок, картинка в ответе или номер.

Роутер обслуживает личные чаты и группы (`/cmp`, `/cmp@bot`, `/cmp #номер`);
каналы исключены — там нет ни альбомов от пользователей, ни нормальных ответов
на сообщения. Подключается **раньше** `private` и `group`: catch-all
`F.text.startswith("/")` в `private` и обработчик `F.text` в `group` иначе съели
бы `/cmp`, а фото и документы ушли бы в DeepSeek.

Картинки перехватываются только в двух случаях: у сообщения есть подпись/текст
`/cmp` или это продолжение нашего альбома (фильтр `AlbumContinuation`). Третье
фото уже сравнённого альбома проходит фильтр, но сервис молча дропает его:
в DeepSeek оно тоже не попадает.
"""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, Filter
from aiogram.types import Message

from ..compare import api as api_module
from ..compare import images, service
from ..config import settings
from ..respond import deny, is_allowed

log = logging.getLogger(__name__)

router = Router(name="compare")

#: Где работает `/cmp`: личка и группы. Каналы не слушаем: там нет альбомов
#: от пользователей и нормальных ответов на сообщения.
CMP_CHATS = frozenset({ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP})
router.message.filter(F.chat.type.in_(CMP_CHATS))

_URL_RE = re.compile(r"https?://\S+")
# Первый `#номер` сравнения: три и более цифр (`/cmp #337326`).
_ID_RE = re.compile(r"#(\d{3,})")
# Хвостовая пунктуация из текста сообщения не часть ссылки.
_URL_TAIL = ".,;"


class AlbumContinuation(Filter):
    """Пропускает фото/документ с нашим ключом: идёт сборка или шло сравнение."""

    async def __call__(self, message: Message) -> bool:
        return service.album_pending(
            message.chat.id,
            getattr(message, "media_group_id", None),
            message.message_id,
        )


async def _download_url(url: str) -> bytes:
    """Скачивает картинку по ссылке с теми же лимитами, что и `/cmp`."""
    api = api_module.get_api()
    return await images.download_url(
        api.session,
        url,
        max_bytes=settings.cmp_max_bytes,
        timeout=15.0,
        allow_private=settings.cmp_allow_private_hosts,
    )


def _reply_image(message: Message):
    """Сообщение-ответ, если в нём есть фото/документ (иначе None)."""
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return None
    if getattr(reply, "photo", None) or getattr(reply, "document", None):
        return reply
    return None


@router.message(Command("cmp"))
async def on_cmp(message: Message) -> None:
    """`/cmp` со ссылками, подписью к картинке, ответом на фото или без аргументов."""
    user = message.from_user
    if user is None:
        return
    if not is_allowed(user.id):
        return await deny(message)

    text = message.text or message.caption or ""
    urls = [url.rstrip(_URL_TAIL) for url in _URL_RE.findall(text)]
    urls = [url for url in urls if url]
    reply = _reply_image(message)
    own_image = bool(getattr(message, "photo", None) or getattr(message, "document", None))
    chat_type = getattr(message.chat.type, "value", message.chat.type)

    # URL из текста не логируем: в них бывает `startapp`-токен. Длины и флаги
    # достаточно, чтобы понять, какая ветка выбрана и почему.
    log.info(
        "cmp: вход chat=%s/%s user=%s text=%d urls=%d own_image=%s reply=%s",
        message.chat.id,
        chat_type,
        user.id,
        len(text),
        len(urls),
        own_image,
        reply is not None,
    )

    try:
        # Две ссылки — сразу сравниваем, даже если в сообщении/ответе есть картинка.
        if len(urls) >= 2:
            await service.compare_urls(message, urls[:2])
            return

        # Номер из кеша: `/cmp #337326` — раньше картинок и ответов (но после двух ссылок).
        match = _ID_RE.search(text)
        if match is not None:
            await service.resend_cached(message, match.group(1))
            return

        if own_image:
            if urls:
                # Подпись /cmp к картинке + одна ссылка: сравниваем сразу.
                first = await service.download_message_image(message)
                await service.compare_images(
                    message, first, await _download_url(urls[0])
                )
            else:
                # Картинка с подписью /cmp — старт альбома, ждём остальные.
                await service.arm_album(message)
            return

        if reply is not None and urls:
            # Картинка в ответе + одна ссылка: сравниваем сразу.
            first = await service.download_message_image(reply)
            await service.compare_images(message, first, await _download_url(urls[0]))
            return
    except images.ImageError as exc:
        # Текст уже человеческий («не картинка», «больше 20 МБ», «не скачалось»).
        await message.answer(str(exc))
        return

    # Ни двух ссылок, ни картинки с подписью: подсказываем формат, состояние не держим.
    # В группе подсказка дополняется абзацем про privacy mode (`_need_two_text`).
    await message.answer(service._need_two_text(message))


@router.message(F.photo | F.document, AlbumContinuation())
async def on_album_image(message: Message) -> None:
    """Следующая картинка уже начатого альбома (обычно без подписи)."""
    user = message.from_user
    if user is None:
        return
    if not is_allowed(user.id):
        return await deny(message)
    await service.add_album_image(message)


__all__ = ["AlbumContinuation", "CMP_CHATS", "on_album_image", "on_cmp", "router"]
