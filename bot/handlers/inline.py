"""Inline-режим: @username запрос в любом чате.

Telegram ждёт результат inline-запроса около 10 секунд, после чего query ID
протухает (``QUERY_ID_INVALID``). DeepSeek отвечает медленнее, поэтому здесь:

* ответ стримится из DeepSeek, и частичный результат показывается сразу
  (``INLINE_EARLY_UPDATE``) — пользователь видит текст за секунды, а не за 10;
* длина ответа ограничена (``INLINE_MAX_TOKENS``) — короче значит быстрее;
* действует жёсткий дедлайн (``INLINE_TIMEOUT``): что успели, то и отдаём;
* устаревшие запросы отменяются — обрабатывается только последний от пользователя;
* режим рассуждений принудительно выключен (с ним успеть невозможно);
* ответы кэшируются в памяти (``INLINE_CACHE_TTL``), плюс их кэширует сам Telegram;
* слишком короткие запросы не тратят квоту DeepSeek;
* в выдаче только JPEG-картинки: превью в PNG/WebP Telegram молча ломает;
* каждая операция пишет замеры времени в лог (ttft / first update / total).
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
from ..utils import extract_image_urls, is_jpeg_url, split_message, telegram_html

log = logging.getLogger(__name__)

router = Router(name="inline")

# Не больше этого числа картинок в выдаче: каждая заставляет Telegram скачивать превью.
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


def _photo_urls(answer: str) -> list[str]:
    """Ссылки, которые можно отдать как inline-картинки.

    Оставляем только JPEG: превью в PNG/WebP Telegram молча не показывает.
    """
    candidates = extract_image_urls(answer, limit=MAX_PHOTO_RESULTS * 4)
    jpeg = [url for url in candidates if is_jpeg_url(url)]
    if len(jpeg) < len(candidates):
        log.debug("inline: отброшено %d не-JPEG картинок", len(candidates) - len(jpeg))
    return jpeg[:MAX_PHOTO_RESULTS]


def _build_results(
    answer: str,
    *,
    with_photos: bool,
    in_progress: bool = False,
) -> tuple[list, int]:
    """Собирает выдачу. Возвращает (результаты, сколько картинок)."""
    first_chunk = split_message(answer)[0] if answer else ""
    message_text, parse_mode = telegram_html(first_chunk)
    title = (answer.splitlines()[0].strip() if answer else "")[:64] or "Ответ DeepSeek"
    if in_progress:
        title = f"⏳ {title}"[:64]
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

    photos = 0
    if with_photos:
        caption = html.escape(" ".join(answer.split())[:900])
        for url in _photo_urls(answer):
            results.append(
                InlineQueryResultPhoto(
                    id=f"photo-{photos}",
                    photo_url=url,
                    thumbnail_url=url,
                    title="🖼 Картинка из ответа",
                    description=url[:128],
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                )
            )
            photos += 1
    return results, photos


async def _answer(query: InlineQuery, results: list, **kwargs: Any) -> None:
    """Отправляет результат, молча переживая протухший query ID."""
    kwargs.setdefault("cache_time", 0)
    kwargs.setdefault("is_personal", True)
    try:
        await query.answer(results, **kwargs)
    except TelegramBadRequest as exc:
        # Обычная ситуация при быстром наборе: пользователь уже изменил запрос.
        log.debug("Inline-запрос устарел, ответ не отправлен: %s", exc)


def _deadline_hint() -> list[InlineQueryResultArticle]:
    return _hint(
        "⏳ Не успеваю за лимит inline",
        f"Запрос считается дольше {settings.inline_timeout:g} с. "
        f"Откройте чат с ботом — @{settings.bot_username} — там отвечу без спешки.",
    )


async def _process(query: InlineQuery, text: str) -> None:
    """Считает ответ в DeepSeek, показывая частичный результат по ходу."""
    user_id = query.from_user.id
    tag = query.id[:8]
    started = time.perf_counter()

    cached = _cache_get(user_id, text)
    if cached is not None:
        results, photos = _build_results(cached, with_photos=True)
        await _answer(query, results, cache_time=settings.inline_cache_ttl)
        log.info(
            "inline[%s] user=%d: cache_hit total=%.3fs chars=%d photos=%d",
            tag, user_id, time.perf_counter() - started, len(cached), photos,
        )
        return

    answer = ""
    ttft: float | None = None
    early_at: float | None = None
    early_sent = False

    async def consume() -> None:
        nonlocal answer, ttft, early_at, early_sent
        async for piece in deepseek.stream(
            text,
            images=extract_image_urls(text, limit=5) or None,
            thinking=False,
            max_tokens=settings.inline_max_tokens,
            system=INLINE_SYSTEM,
        ):
            if ttft is None:
                ttft = time.perf_counter() - started
            answer += piece
            if (
                settings.inline_early_update
                and not early_sent
                and (
                    len(answer) >= settings.inline_early_min_chars
                    or time.perf_counter() - started >= settings.inline_early_delay
                )
            ):
                early_sent = True
                early_at = time.perf_counter() - started
                results, _ = _build_results(answer, with_photos=False, in_progress=True)
                await _answer(query, results, cache_time=0)

    try:
        await asyncio.wait_for(consume(), timeout=settings.inline_timeout)
    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - started
        if answer.strip():
            results, _ = _build_results(answer, with_photos=False, in_progress=True)
            await _answer(query, results, cache_time=0, button=_OPEN_CHAT_BUTTON)
            log.info(
                "inline[%s] user=%d: deadline_partial ttft=%.2fs early=%.2fs total=%.2fs chars=%d",
                tag, user_id, ttft if ttft is not None else -1,
                early_at if early_at is not None else -1, elapsed, len(answer),
            )
        else:
            await _answer(query, _deadline_hint(), button=_OPEN_CHAT_BUTTON)
            log.info(
                "inline[%s] user=%d: deadline_empty ttft=-1 early=%.2fs total=%.2fs chars=0",
                tag, user_id, early_at if early_at is not None else -1, elapsed,
            )
        return
    except DeepSeekError as exc:
        await _answer(query, _hint("⚠️ Ошибка", str(exc)[:200]))
        log.warning(
            "inline[%s] user=%d: error total=%.2fs %s",
            tag, user_id, time.perf_counter() - started, exc,
        )
        return

    if not answer.strip():
        await _answer(query, _hint("⚠️ Пустой ответ", "Модель не вернула текст, попробуйте ещё раз."))
        log.info("inline[%s] user=%d: empty total=%.2fs", tag, user_id, time.perf_counter() - started)
        return

    _cache_put(user_id, text, answer)
    results, photos = _build_results(answer, with_photos=True)
    await _answer(query, results, cache_time=settings.inline_cache_ttl)
    log.info(
        "inline[%s] user=%d: ok ttft=%.2fs early=%.2fs total=%.2fs chars=%d photos=%d",
        tag, user_id, ttft if ttft is not None else -1,
        early_at if early_at is not None else -1,
        time.perf_counter() - started, len(answer), photos,
    )


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
        log.debug("inline: предыдущий запрос пользователя %d отменён", user_id)

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
