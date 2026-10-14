"""Inline-режим: @username запрос в любом чате."""

from __future__ import annotations

import html
import logging

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
)

from ..config import settings
from ..deepseek import DeepSeekError, deepseek
from ..respond import is_allowed
from ..state import is_thinking
from ..utils import extract_image_urls, split_message, telegram_html

log = logging.getLogger(__name__)

router = Router(name="inline")

MAX_PHOTO_RESULTS = 10


def _hint(title: str, description: str) -> list[InlineQueryResultArticle]:
    return [
        InlineQueryResultArticle(
            id="hint",
            title=title,
            description=description,
            input_message_content=InputTextMessageContent(message_text=f"{title}\n{description}"),
        )
    ]


@router.inline_query()
async def on_inline_query(query: InlineQuery) -> None:
    if not is_allowed(query.from_user.id):
        await query.answer(
            _hint("🚫 Доступ ограничен", "Этот бот принимает запросы только от разрешённых пользователей."),
            cache_time=0,
            is_personal=True,
        )
        return

    text = (query.query or "").strip()
    if not text:
        await query.answer(
            _hint(
                "Введите запрос",
                f"Например: @{settings.bot_username} объясни, что такое обратная матрица",
            ),
            cache_time=0,
            is_personal=True,
        )
        return

    try:
        answer = await deepseek.ask(
            text,
            images=extract_image_urls(text, limit=5) or None,
            thinking=is_thinking(query.from_user.id),
        )
    except DeepSeekError as exc:
        await query.answer(
            _hint("⚠️ Ошибка", str(exc)[:200]),
            cache_time=0,
            is_personal=True,
        )
        return

    first_chunk = split_message(answer)[0] if answer else ""
    message_text, parse_mode = telegram_html(first_chunk)
    title = (answer.splitlines()[0].strip() if answer else "")[:64] or "Ответ DeepSeek"
    description = " ".join(answer.split())[:128]

    results: list = [
        InlineQueryResultArticle(
            id="answer",
            title=title,
            description=description,
            input_message_content=InputTextMessageContent(
                message_text=message_text,
                parse_mode=parse_mode,
            ),
        )
    ]

    caption = html.escape(" ".join(answer.split())[:900])
    for index, url in enumerate(extract_image_urls(answer, limit=MAX_PHOTO_RESULTS)):
        results.append(
            InlineQueryResultPhoto(
                id=f"photo-{index}",
                photo_url=url,
                thumbnail_url=url,
                title="🖼 Картинка из ответа",
                description=url[:128],
                caption=caption or None,
                parse_mode=ParseMode.HTML,
            )
        )

    await query.answer(results, cache_time=30, is_personal=True)
