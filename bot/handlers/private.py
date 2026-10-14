"""Обработчики личных сообщений."""

from __future__ import annotations

import base64
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from ..config import settings
from ..keyboards import think_keyboard
from ..respond import (
    MAX_DOWNLOAD_BYTES,
    ask_and_reply,
    deny,
    download_photo,
    is_allowed,
)
from ..state import reset_user, toggle_thinking
from ..utils import extract_image_urls

log = logging.getLogger(__name__)

router = Router(name="private")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

HELP_TEXT = (
    "Я отвечаю на вопросы через DeepSeek.\n\n"
    "• Напишите мне любой текст — и получите ответ.\n"
    "• Пришлите фото (или фото с подписью-вопросом) — я разберу изображение.\n"
    "• Можно указать ссылку на картинку прямо в тексте запроса.\n"
    f"• В любом чате наберите @{settings.bot_username} и запрос — я отвечу прямо там.\n\n"
    "Команды:\n"
    "/think — режим рассуждений (для сложных задач, медленнее)\n"
    "/reset — сбросить настройки\n"
    "/help — эта справка"
)


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return await deny(message)
    await message.answer(
        f"Привет! Я бот на DeepSeek. Напишите вопрос или пришлите изображение.\n\n{HELP_TEXT}",
        reply_markup=think_keyboard(message.from_user.id),
    )


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return await deny(message)
    await message.answer(HELP_TEXT)


@router.message(Command("think"))
async def on_think(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return await deny(message)
    enabled = toggle_thinking(message.from_user.id)
    text = (
        "🧠 Режим рассуждений включён — отвечаю точнее, но медленнее."
        if enabled
        else "⚡ Режим рассуждений выключен — отвечаю быстро."
    )
    await message.answer(text, reply_markup=think_keyboard(message.from_user.id))


@router.message(Command("reset"))
async def on_reset(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return await deny(message)
    reset_user(message.from_user.id)
    await message.answer("Настройки сброшены.", reply_markup=think_keyboard(message.from_user.id))


@router.callback_query(F.data == "toggle_think")
async def on_toggle_think(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        return await callback.answer("Доступ ограничен", show_alert=True)
    enabled = toggle_thinking(callback.from_user.id)
    await callback.answer("Рассуждения включены" if enabled else "Рассуждения выключены")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=think_keyboard(callback.from_user.id))


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return await deny(message)
    photo = message.photo[-1]
    try:
        raw = await download_photo(message, photo.file_id)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось скачать фото")
        return await message.reply("Не удалось скачать изображение, попробуйте ещё раз.")
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    prompt = (message.caption or "").strip() or "Опиши изображение и выдели главное."
    await ask_and_reply(message, prompt, images=[data_url])


@router.message(F.document)
async def on_document(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return await deny(message)
    document = message.document
    if not (document.mime_type or "").startswith("image/"):
        return await message.reply("Поддерживаются только изображения (JPEG, PNG, GIF, WebP).")
    if document.file_size and document.file_size > MAX_DOWNLOAD_BYTES:
        return await message.reply("Файл больше 20 МБ — Telegram не даст мне его скачать.")
    try:
        raw = await download_photo(message, document.file_id)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось скачать документ")
        return await message.reply("Не удалось скачать файл, попробуйте ещё раз.")
    data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    prompt = (message.caption or "").strip() or "Опиши изображение и выдели главное."
    await ask_and_reply(message, prompt, images=[data_url])


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return await deny(message)
    images = extract_image_urls(message.text, limit=5) or None
    await ask_and_reply(message, message.text, images=images)


@router.message(F.text.startswith("/"))
async def on_unknown_command(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return await deny(message)
    await message.reply("Неизвестная команда. Отправьте /help.")
