"""Хендлеры `/cmp`: две ссылки, две картинки или картинка в ответе.

Роутер подключается **раньше** `private`: там есть catch-all
`F.text.startswith("/")`, который иначе съел бы `/cmp`. Свой фильтр
`AwaitingImages` нужен по той же причине: в `private` фото и документы уходят
в DeepSeek, а здесь — только пока пользователь начал сравнение.
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
router.message.filter(F.chat.type == ChatType.PRIVATE)

_URL_RE = re.compile(r"https?://\S+")
# Хвостовая пунктуация из текста сообщения не часть ссылки.
_URL_TAIL = ".,;"


class AwaitingImages(Filter):
    """Пропускает фото/документ, только если пользователь уже начал сравнение."""

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        return bool(user and service.is_waiting(user.id))


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
    """`/cmp` со ссылками, в ответе на фото или без аргументов (ждём две картинки)."""
    user = message.from_user
    if user is None:
        return
    if not is_allowed(user.id):
        return await deny(message)

    text = message.text or message.caption or ""
    urls = [url.rstrip(_URL_TAIL) for url in _URL_RE.findall(text)]
    urls = [url for url in urls if url]
    reply = _reply_image(message)

    try:
        # Две ссылки — сразу сравниваем, даже если в ответе висит картинка.
        if len(urls) >= 2:
            await service.compare_urls(message, urls[:2])
            return

        if reply is not None:
            first = await service.download_message_image(reply)
            if urls:
                # Картинка в ответе + одна ссылка: сравниваем сразу.
                await service.compare_images(message, first, await _download_url(urls[0]))
            else:
                await service.start_with(message, first)
            return

        if urls:
            await service.start_with(message, await _download_url(urls[0]))
            return
    except images.ImageError as exc:
        # Текст уже человеческий («не картинка», «больше 20 МБ», «не скачалось»).
        await message.answer(str(exc))
        return

    service.start_empty(user.id)
    await message.answer(service.START_INSTRUCTION)


@router.message(F.photo | F.document, AwaitingImages())
async def on_image(message: Message) -> None:
    """Вторая (или первая после пустого `/cmp`) картинка в текущем ожидании."""
    user = message.from_user
    if user is None:
        return
    if not is_allowed(user.id):
        return await deny(message)
    await service.add_image(message)


__all__ = ["AwaitingImages", "on_cmp", "on_image", "router"]
