"""Общая логика ответов: доступ, скачивание фото, отправка ответа."""

from __future__ import annotations

import logging
from io import BytesIO

from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from .config import settings
from .deepseek import DeepSeekError, deepseek
from .state import is_thinking
from .utils import extract_image_urls, split_message, telegram_html

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
CHAT_IMAGE_PREVIEWS = 3


def is_allowed(user_id: int) -> bool:
    allowed = settings.allowed_ids
    return not allowed or user_id in allowed


async def deny(message: Message) -> None:
    await message.reply("🚫 Доступ к боту ограничен списком разрешённых пользователей.")


async def download_photo(message: Message, file_id: str) -> bytes:
    file = await message.bot.get_file(file_id)
    buffer = BytesIO()
    await message.bot.download_file(file.file_path, buffer)
    return buffer.getvalue()


async def ask_and_reply(
    message: Message,
    prompt: str,
    images: list[str] | None = None,
) -> None:
    """Отправляет запрос в DeepSeek и публикует ответ в чат."""
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    try:
        answer = await deepseek.ask(
            prompt,
            images=images,
            thinking=is_thinking(message.from_user.id),
        )
    except DeepSeekError as exc:
        await message.reply(f"⚠️ {exc}")
        return

    chunks = split_message(answer)
    for index, chunk in enumerate(chunks):
        content, parse_mode = telegram_html(chunk)
        extra: dict = {}
        if index == 0:
            extra["reply_to_message_id"] = message.message_id
        try:
            await message.answer(content, parse_mode=parse_mode, **extra)
        except TelegramBadRequest:
            await message.answer(chunk, parse_mode=None, **extra)

    for url in extract_image_urls(answer, limit=CHAT_IMAGE_PREVIEWS):
        try:
            await message.answer_photo(url)
        except TelegramBadRequest:
            log.info("Не удалось отправить картинку %s", url)
