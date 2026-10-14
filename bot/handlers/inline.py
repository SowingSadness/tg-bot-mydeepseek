"""Inline-режим: @username запрос в любом чате.

Telegram ждёт результат inline-запроса около 10 секунд, после чего query ID
протухает. DeepSeek отвечает медленнее, поэтому здесь:

* длина ответа ограничена (``INLINE_MAX_TOKENS``) — короче значит быстрее;
* действует жёсткий дедлайн (``INLINE_TIMEOUT``) с понятной подсказкой при промахе;
* устаревшие запросы отменяются — обрабатывается только последний от пользователя;
* режим рассуждений принудительно выключен (с ним успеть невозможно);
* ошибки не засоряют лог трейсбеками.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
)

from ..config import settings
from ..deepseek import DeepSeekError, deepseek
from ..respond import is_allowed
from ..utils import extract_image_urls, split_message, telegram_html

log = logging.getLogger(__name__)

router = Router(name="inline")

MAX_PHOTO_RESULTS = 10

# Жёсткий дедлайн на весь запрос. Лимит Telegram — около 10 с; 8.5 с оставляет
# запас на сетевой round-trip к Telegram при отправке ответа.
INLINE_TIMEOUT = 8.5

# Короткие ответы генерируются быстрее и лучше смотрятся в подсказке inline.
INLINE_MAX_TOKENS = 350

INLINE_SYSTEM = (
    "Отвечай кратко и по делу: только суть, без вступлений и длинных пояснений. "
    "Если уместен список — используй короткие пункты. Не предлагай и не вставляй изображения."
)

# Последний незавершённый запрос каждого пользователя (для отмены устаревших).
_inflight: dict[int, asyncio.Task] = {}


def _hint(title: str, description: str) -> list[InlineQueryResultArticle]:
    return [
        InlineQueryResultArticle(
            id="hint",
            title=title,
            description=description,
            input_message_content=InputTextMessageContent(message_text=f"{title}\n{description}"),
        )
    ]


async def _answer(query: InlineQuery, results: list, **kwargs: Any) -> None:
    """Отправляет результат, молча переживая протухший query ID."""
    kwargs.setdefault("cache_time", 0)
    kwargs.setdefault("is_personal", True)
    try:
        await query.answer(results, **kwargs)
    except TelegramBadRequest as exc:
        # Обычная ситуация при быстром наборе: пользователь уже изменил запрос.
        log.debug("Inline-запрос устарел, ответ не отправлен: %s", exc)


async def _process(query: InlineQuery, text: str) -> None:
    """Считает ответ в DeepSeek в рамках дедлайна и отдаёт его Telegram."""
    try:
        answer = await asyncio.wait_for(
            deepseek.ask(
                text,
                images=extract_image_urls(text, limit=5) or None,
                thinking=False,
                max_tokens=INLINE_MAX_TOKENS,
                system=INLINE_SYSTEM,
            ),
            timeout=INLINE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await _answer(
            query,
            _hint(
                "⏳ Не успеваю за лимит inline",
                f"Запрос считается дольше {INLINE_TIMEOUT:g} с. "
                f"Напишите боту в личный чат — @{settings.bot_username} — там отвечу без спешки.",
            ),
        )
        return
    except DeepSeekError as exc:
        await _answer(query, _hint("⚠️ Ошибка", str(exc)[:200]))
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

    await _answer(query, results, cache_time=30)


@router.inline_query()
async def on_inline_query(query: InlineQuery) -> None:
    if not is_allowed(query.from_user.id):
        await _answer(
            query,
            _hint("🚫 Доступ ограничен", "Этот бот принимает запросы только от разрешённых пользователей."),
        )
        return

    text = (query.query or "").strip()
    if not text:
        await _answer(
            query,
            _hint(
                "Введите запрос",
                f"Например: @{settings.bot_username} объясни, что такое обратная матрица",
            ),
        )
        return

    user_id = query.from_user.id

    # Отменяем предыдущий запрос этого пользователя: пока печатается текст,
    # Telegram присылает новый inline-запрос на каждую паузу.
    previous = _inflight.pop(user_id, None)
    if previous is not None and not previous.done():
        previous.cancel()

    task = asyncio.create_task(_process(query, text))
    _inflight[user_id] = task
    try:
        await task
    except asyncio.CancelledError:
        # Запрос вытеснен более новым — тихо выходим, без логов и ошибок.
        return
    except Exception:  # noqa: BLE001
        log.exception("Не удалось обработать inline-запрос")
    finally:
        if _inflight.get(user_id) is task:
            _inflight.pop(user_id, None)
