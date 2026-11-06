"""Inline по номеру сравнения: ``@bot #482913`` → фото «A|B», кнопка и бабл.

Inline-режим обязан ответить примерно за 10 секунд, поэтому здесь нет ни сети,
ни DeepSeek, ни кодирования картинок: только ``SELECT`` из SQLite и
``build.render_cached`` (пересборка HTML из уже сжатых WebP за миллисекунды).

Выдача на найденный номер — до трёх результатов в таком порядке:
фото-композит «Вставить превью» с url-кнопкой «Открыть сравнение»
(если есть ``preview_file_id``), article «Открыть сравнение» с той же
кнопкой (если задан ``BOT_USERNAME`` — а он обязателен) и текстовый
фолбэк — **только** когда нет ни фото, ни кнопки.

Кнопка не зависит от ``CMP_WEBAPP_SHORT_NAME``: пустое значение — валидная
конфигурация для Main Mini App, ссылка получается ``https://t.me/<bot>?startapp=…``;
заданное — ``https://t.me/<bot>/<short>?startapp=…``.

Шторка (widget-контент) в inline отдаётся только при ``CMP_INLINE_WIDGET=true``:
бета Bot API отклоняет widget в inline-выдаче (``INPUT_MEDIA_WIDGET_NOT_ALLOWED``),
поэтому по умолчанию её нет, а при включённом флаге отказ сервера приводит к
повтору без шторки (фото и кнопка остаются).

Голый ``#`` и номера короче шести цифр — подсказка; всё, что не похоже на
``#<6-8 цифр>``, отдаём обычному DeepSeek-пути (``False``).
"""

from __future__ import annotations

import logging
import re

from ..config import settings
from . import api as api_module
from . import build, service, webauth
from .store import Cached

log = logging.getLogger(__name__)

#: Настоящий номер сравнения: ``#`` и 6-8 цифр (номера могут удлиняться при коллизиях).
ID_RE = re.compile(r"^#(\d{6,8})$")

#: Всё, что Telegram-пользователь мог набрать как «номер»: ``#`` + до 8 цифр.
HINT_RE = re.compile(r"^#\d{0,8}$")

MISSING_DESCRIPTION = "Номер не найден или истёк. Сделайте сравнение в личке с ботом."


def _start_token(cmp_id: str) -> str:
    """Подпись для ``startapp``: ``<id>_<20 симв. base64url>`` (≤ 64 символов).

    Тонкая обёртка над `webauth.sign`: тот же HMAC-SHA256 от токена бота и
    номера, поэтому уже отправленные в чат кнопки продолжают работать, а
    формула подписи живёт в одном месте с проверкой мини-аппа.
    """
    return webauth.sign(cmp_id)


def _webapp_button(cached: Cached) -> dict | None:
    """url-кнопка «Открыть сравнение» на мини-апп или ``None`` без ``BOT_USERNAME``.

    Форма ссылки одна на всю выдачу: с short name — отдельное приложение
    (``t.me/<bot>/<short>?startapp=…``), без него — Main Mini App
    (``t.me/<bot>?startapp=…``, ``messages.requestMainWebView``).
    """
    bot = settings.bot_username
    if not bot:
        return None
    token = _start_token(cached.id)
    short = settings.cmp_webapp_short_name
    if short:
        link = f"https://t.me/{bot}/{short}?startapp={token}"
    else:
        link = f"https://t.me/{bot}?startapp={token}"
    return {"inline_keyboard": [[{"text": "Открыть сравнение", "url": link}]]}


def _photo_result(cached: Cached) -> dict:
    """``InlineQueryResultCachedPhoto``: композит «A|B» с кнопкой мини-аппа.

    Работает только для сравнений, у которых уже есть ``preview_file_id``
    (его сохраняет `/cmp` после загрузки композита в личку). Заголовок
    «Вставить превью» говорит, что произойдёт при выборе пункта.
    """
    first, second = settings.cmp_labels_pair
    caption = f"Сравнение #{cached.id} · {first} / {second}"
    result = {
        "type": "photo",
        "id": f"{cached.id}-photo",
        "photo_file_id": cached.preview_file_id,
        "title": "Вставить превью",
        "description": caption,
        "caption": caption,
    }
    button = _webapp_button(cached)
    if button is not None:
        result["reply_markup"] = button
    return result


def _open_result(cached: Cached) -> dict:
    """Article с url-кнопкой на мини-апп (``web_app`` в inline запрещён)."""
    first, second = settings.cmp_labels_pair
    return {
        "type": "article",
        "id": f"{cached.id}-open",
        "title": "Открыть сравнение",
        "description": "Полноразмерное сравнение в мини-приложении",
        "input_message_content": {
            "message_text": f"Сравнение #{cached.id} · {first} / {second}",
        },
        "reply_markup": _webapp_button(cached),
    }


def _text_result(cached: Cached) -> dict:
    """Текст на случай, когда нет ни превью, ни возможности открыть мини-апп."""
    first, second = settings.cmp_labels_pair
    reasons: list[str] = []
    if not cached.preview_file_id:
        reasons.append("превью для этого номера ещё не готово")
    if not settings.bot_username:
        reasons.append("мини-приложение не настроено (пустой BOT_USERNAME)")
    if not reasons:  # защита: без превью и кнопки выдача была бы пустой
        reasons.append("фото и кнопка недоступны")
    tail = (
        "Причина: " + "; ".join(reasons)
        + ". Откройте бота в личке и пришлите две картинки командой /cmp."
    )
    return {
        "type": "article",
        "id": f"{cached.id}-text",
        "title": f"Сравнение #{cached.id} · только текст",
        "description": f"{first} / {second}",
        "input_message_content": {
            "message_text": f"Сравнение #{cached.id} · {first} / {second}\n{tail}",
        },
    }


def _found_results(cached: Cached) -> list[dict]:
    """Результаты для найденного сравнения: фото → кнопка → текст (→ шторка).

    Текстовый фолбэк добавляется **только** если показать больше нечего: нет
    ни ``preview_file_id``, ни url-кнопки (``BOT_USERNAME`` пуст). Шторка идёт
    последней и только при ``CMP_INLINE_WIDGET=true``: бета Bot API отклоняет
    widget в inline.
    """
    results: list[dict] = []
    if cached.preview_file_id:
        results.append(_photo_result(cached))
    if _webapp_button(cached) is not None:
        results.append(_open_result(cached))
    if not results:
        results.append(_text_result(cached))
    if settings.cmp_inline_widget:
        results.append(_found_result(cached))
    return results


def _found_result(cached: Cached) -> dict:
    """Один article, у которого ``input_message_content`` — поля виджета."""
    html = build.render_cached(
        image_a=cached.image_a,
        image_b=cached.image_b,
        labels=settings.cmp_labels_pair,
        title=settings.cmp_title,
        hint=settings.cmp_hint,
        # Старые номера пересобираются в своих пропорциях, а не в текущих по умолчанию.
        aspect=cached.aspect,
        width=cached.width,
    )
    first, second = settings.cmp_labels_pair
    return {
        "type": "article",
        "id": cached.id,
        "title": f"Сравнение #{cached.id}",
        "description": f"{first} / {second} · {cached.width} px",
        "input_message_content": {"html": html, "width": 0, "height": 0},
    }


def _missing_result(digits: str) -> dict:
    """Номер не найден (или чужой): показываем подсказку, а не пустую выдачу."""
    return {
        "type": "article",
        "id": "cmp-missing",
        "title": f"Сравнение #{digits} не найдено",
        "description": MISSING_DESCRIPTION,
        "input_message_content": {
            "message_text": (
                f"Сравнение #{digits} не найдено или уже удалено. "
                "Откройте бота в личке и пришлите две картинки командой /cmp."
            ),
        },
    }


def _hint_result() -> dict:
    """Голый ``#`` или слишком короткий номер: объясняем, что вводить."""
    return {
        "type": "article",
        "id": "cmp-hint",
        "title": "Введите номер сравнения",
        "description": "Например: #482913",
        "input_message_content": {
            "message_text": (
                "Например: #482913. Номера выдаёт бот в личке после /cmp."
            ),
        },
    }


async def _answer(api, query, results: list[dict]) -> None:
    """Отправляет результаты как есть (aiogram не знает widget-контент)."""
    await api.answer_inline(query.id, results, cache_time=0, is_personal=True)


async def try_answer(query) -> bool:
    """Обрабатывает ``#номер``; ``True`` — DeepSeek звать не нужно.

    ``False`` возвращается только для обычных запросов и когда кеш/Bot API
    не сконфигурированы: тогда пусть работает прежний DeepSeek-путь.
    """
    text = (query.query or "").strip()
    if not HINT_RE.fullmatch(text):
        return False

    tag = str(getattr(query, "id", ""))[:8]

    try:
        api = api_module.get_api()
    except RuntimeError as exc:
        # Без Bot API виджет-ответ отправить нечем — не ломаем обычный inline.
        log.warning("inline[%s]: Bot API не настроен, '%s' не обработан: %s", tag, text, exc)
        return False

    try:
        match = ID_RE.fullmatch(text)
        if match is None:
            await _answer(api, query, [_hint_result()])
            log.info("inline[%s]: подсказка по номеру для '%s'", tag, text)
            return True

        digits = match.group(1)
        try:
            store = service.get_store()
        except RuntimeError as exc:
            log.warning("inline[%s]: кеш сравнений не настроен: %s", tag, exc)
            return False

        owner_id = None if settings.cmp_share_by_id else query.from_user.id
        cached = await store.get(digits, owner_id=owner_id)
        if cached is None:
            await _answer(api, query, [_missing_result(digits)])
            log.info("inline[%s]: сравнение #%s не найдено (owner=%s)", tag, digits, owner_id)
            return True

        results = _found_results(cached)
        try:
            await _answer(api, query, results)
        except api_module.WidgetAPIError:
            # Сервер не принял widget-контент (INPUT_MEDIA_WIDGET_NOT_ALLOWED):
            # повторяем без шторки — фото и кнопка остаются.
            fallback = [result for result in results if result["id"] != cached.id]
            if len(fallback) == len(results):
                raise
            await _answer(api, query, fallback)
            log.info("inline[%s]: сервер отклонил шторку, выдача без виджета", tag)
            results = fallback
        log.info(
            "inline[%s]: сравнение #%s из кеша (width=%d, quality=%d, zlib=%d, results=%s)",
            tag, cached.id, cached.width, cached.quality, cached.zlib_bytes,
            [result["id"] for result in results],
        )
        return True
    except api_module.WidgetAPIError as exc:
        # Протухший query ID — обычное дело; главное не улететь в DeepSeek.
        log.warning("inline[%s]: не удалось ответить на '%s': %s", tag, text, exc)
        return True


__all__ = ["HINT_RE", "ID_RE", "try_answer"]
