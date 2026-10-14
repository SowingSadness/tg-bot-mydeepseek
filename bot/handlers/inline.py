"""Inline-режим: @username запрос в любом чате.

Telegram ждёт результат inline-запроса около 10 секунд, после чего query ID
протухает (``QUERY_ID_INVALID``). DeepSeek отвечает медленнее, поэтому здесь:

* длина ответа ограничена (``INLINE_MAX_TOKENS``) — короче значит быстрее;
* действует жёсткий дедлайн (``INLINE_TIMEOUT``) с понятной подсказкой;
* устаревшие запросы отменяются — обрабатывается только последний от пользователя;
* режим рассуждений принудительно выключен (с ним успеть невозможно);
* ответы кэшируются в памяти (``INLINE_CACHE_TTL``), плюс их кэширует сам Telegram;
* слишком короткие запросы не тратят квоту DeepSeek;
* в выдаче минимум результатов, чтобы клиенту было что отрисовать быстрее;
* ошибки не засоряют лог трейсбеками.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import OrderedDict
from typing import Any

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InlineQueryResultsButton,
    InputTextMessageContent,
)

from ..config import settings
from ..deepseek import DeepSeekError, deepseek
from ..respond import is_allowed
from ..utils import extract_image_urls, split_message, telegram_html

log = logging.getLogger(__name__)

router = Router(name="inline")

# Каждая картинка в выдаче заставляет Telegram скачивать превью, поэтому список
# держим коротким — так результаты появляются быстрее.
MAX_PHOTO_RESULTS = 4

# Короче этого запрос в DeepSeek не отправляем — отвечаем подсказкой.
MIN_QUERY_LEN = 3

INLINE_SYSTEM = (
    "Отвечай кратко и по делу: только суть, без вступлений и длинных пояснений. "
    "Если уместен список — используй короткие пункты. Не предлагай и не вставляй изображения."
)

_OPEN_CHAT_BUTTON = InlineQueryResultsButton(
    text="💬 Открыть чат с ботом",
    start_parameter="inline",
)

# Кэш ответов в памяти: ключ — (пользователь, нормализованный запрос).
# Персональный ключ нужен, чтобы результаты не «протекали» между пользователями
# мимо allowlist.
_CACHE_MAX = 256
_cache: OrderedDict[tuple[int, str], tuple[float, str]] = OrderedDict()

# Последний незавершённый запрос каждого пользователя (для отмены устаревших).
_inflight: dict[int, asyncio.Task] = {}


def _cache_key(user_id: int, text: str) -> tuple[int, str]:
    return user_id, " ".join(text.lower().split())


def _cache_get(user_id: int, text: str) -> str | None:
    key = _cache_key(user_id, text)
    item = _cache.get(key)
    if item is None:
        return None
    stored_at, answer = item
    if time.monotonic() - stored_at > settings.inline_cache_ttl:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return answer


def _cache_put(user_id: int, text: str, answer: str) -> None:
    key = _cache_key(user_id, text)
    _cache[key] = (time.monotonic(), answer)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


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
    user_id = query.from_user.id
    answer = _cache_get(user_id, text)

    if answer is None:
        try:
            answer = await asyncio.wait_for(
                deepseek.ask(
                    text,
                    images=extract_image_urls(text, limit=5) or None,
                    thinking=False,
                    max_tokens=settings.inline_max_tokens,
                    system=INLINE_SYSTEM,
                ),
                timeout=settings.inline_timeout,
            )
        except asyncio.TimeoutError:
            await _answer(
                query,
                _hint(
                    "⏳ Не успеваю за лимит inline",
                    f"Запрос считается дольше {settings.inline_timeout:g} с. "
                    f"Откройте чат с ботом — @{settings.bot_username} — там отвечу без спешки.",
                ),
                button=_OPEN_CHAT_BUTTON,
            )
            return
        except DeepSeekError as exc:
            await _answer(query, _hint("⚠️ Ошибка", str(exc)[:200]))
            return
        _cache_put(user_id, text, answer)

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

    # cache_time позволяет Telegram отдать тот же запрос из своего кэша,
    # не дёргая бота и DeepSeek вообще.
    await _answer(query, results, cache_time=settings.inline_cache_ttl)


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

    if len(text) < MIN_QUERY_LEN:
        await _answer(
            query,
            _hint("Продолжайте писать…", f"Введите минимум {MIN_QUERY_LEN} символа."),
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
