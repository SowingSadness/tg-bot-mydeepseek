"""Обработчики сообщений в группах.

Бот отвечает, только если его упомянули через @username или ответили на его сообщение.
"""

from __future__ import annotations

import base64
import logging
import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from ..config import settings
from ..respond import ask_and_reply, deny, download_photo, is_allowed
from ..utils import extract_image_urls

log = logging.getLogger(__name__)

router = Router(name="group")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

_MENTION = re.compile(rf"@{re.escape(settings.bot_username)}\b", re.IGNORECASE)


def _should_react(message: Message) -> bool:
    text = message.text or message.caption or ""
    if _MENTION.search(text):
        return True
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.is_bot:
        return (reply.from_user.username or "").lower() == settings.bot_username.lower()
    return False


def _clean(text: str) -> str:
    return _MENTION.sub("", text).strip()


@router.message(F.text)
async def on_group_text(message: Message) -> None:
    if not _should_react(message):
        return
    if not is_allowed(message.from_user.id):
        return await deny(message)
    prompt = _clean(message.text) or "Ответь на это сообщение."
    await ask_and_reply(message, prompt, images=extract_image_urls(prompt, limit=5) or None)


@router.message(F.photo)
async def on_group_photo(message: Message) -> None:
    if not _should_react(message):
        return
    if not is_allowed(message.from_user.id):
        return await deny(message)
    photo = message.photo[-1]
    try:
        raw = await download_photo(message, photo.file_id)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось скачать фото")
        return await message.reply("Не удалось скачать изображение.")
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    prompt = _clean(message.caption or "") or "Опиши изображение и выдели главное."
    await ask_and_reply(message, prompt, images=[data_url])


@router.message(F.document)
async def on_group_document(message: Message) -> None:
    if not _should_react(message):
        return
    if not is_allowed(message.from_user.id):
        return await deny(message)
    document = message.document
    if not (document.mime_type or "").startswith("image/"):
        return await message.reply("Поддерживаются только изображения (JPEG, PNG, GIF, WebP).")
    try:
        raw = await download_photo(message, document.file_id)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось скачать документ")
        return await message.reply("Не удалось скачать файл.")
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    prompt = _clean(message.caption or "") or "Опиши изображение и выдели главное."
    await ask_and_reply(message, prompt, images=[data_url])
