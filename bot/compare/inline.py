"""Inline по номеру сравнения: ``@bot #482913`` → тот же бабл из кеша.

Inline-режим обязан ответить примерно за 10 секунд, поэтому здесь нет ни сети,
ни DeepSeek, ни кодирования картинок: только ``SELECT`` из SQLite и
``build.render_cached`` (пересборка HTML из уже сжатых WebP за миллисекунды).

Голый ``#`` и номера короче шести цифр — подсказка; всё, что не похоже на
``#<6-8 цифр>``, отдаём обычному DeepSeek-пути (``False``).
"""

from __future__ import annotations

import logging
import re

from ..config import settings
from . import api as api_module
from . import build, service
from .store import Cached

log = logging.getLogger(__name__)

#: Настоящий номер сравнения: ``#`` и 6-8 цифр (номера могут удлиняться при коллизиях).
ID_RE = re.compile(r"^#(\d{6,8})$")

#: Всё, что Telegram-пользователь мог набрать как «номер»: ``#`` + до 8 цифр.
HINT_RE = re.compile(r"^#\d{0,8}$")

MISSING_DESCRIPTION = "Номер не найден или истёк. Сделайте сравнение в личке с ботом."

#: Бета Bot API не принимает виджеты в inline-выдаче: запоминаем отказ, чтобы
#: не тратить на него запрос при каждом вводе номера (сбрасывается рестартом).
_widgets_blocked = False


def _found_result(cached: Cached) -> dict:
    """Один article, у которого ``input_message_content`` — поля виджета."""
    html = _widget_html(cached)
    first, second = settings.cmp_labels_pair
    return {
        "type": "article",
        "id": cached.id,
        "title": f"Сравнение #{cached.id}",
        "description": f"{first} / {second} · {cached.width} px",
        "input_message_content": {"html": html, "width": 0, "height": 0},
    }


def _widget_html(cached: Cached) -> str:
    """Пересборка HTML-бабла из кеша (миллисекунды, без сети)."""
    return build.render_cached(
        image_a=cached.image_a,
        image_b=cached.image_b,
        labels=settings.cmp_labels_pair,
        title=settings.cmp_title,
        hint=settings.cmp_hint,
        # Старые номера пересобираются в своих пропорциях, а не в текущих по умолчанию.
        aspect=cached.aspect,
        width=cached.width,
    )


async def _answer_widget(api, query, cached: Cached, tag: str) -> bool:
    """Пробует отдать бабл; ``False`` — Telegram виджеты в inline не принимает.

    Бета Bot API отвечает `INPUT_MEDIA_WIDGET_NOT_ALLOWED` на все формы
    widget-контента (проверено: `html`+размер, `type=inputMediaWidget`,
    `type=widget`), а `type: "widget"` в inline-результатах вообще не существует.
    После первого такого отказа флаг `_widgets_blocked` избавляет от лишних
    запросов до перезапуска бота — если поддержку включат, поможет рестарт.
    """
    global _widgets_blocked
    if _widgets_blocked:
        return False

    try:
        await _answer(api, query, [_found_result(cached)])
    except api_module.WidgetAPIError as exc:
        if exc.code == 400 and "WIDGET_NOT_ALLOWED" in (exc.description or ""):
            _widgets_blocked = True
            log.warning(
                "inline[%s]: виджеты в inline-выдаче запрещены Bot API (%s) — "
                "отдаю номер текстом до перезапуска бота",
                tag, exc.description,
            )
        else:
            log.warning(
                "inline[%s]: бабл #%s не принят: код=%s, описание=%s",
                tag, cached.id, exc.code, exc.description,
            )
        return False

    log.info("inline[%s]: бабл #%s отправлен в answerInlineQuery", tag, cached.id)
    return True


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


def _refused_result(cached: Cached) -> dict:
    """Номер есть, но Telegram не вставил бабл в inline-выдачу.

    Виджеты в inline-результатах бета Bot API пока отклоняет
    (`INPUT_MEDIA_WIDGET_NOT_ALLOWED`), поэтому вместо пустой выдачи объясняем,
    где бабл точно откроется: он лежит в кеше и его пришлёт `/cmp #номер`.
    """
    return {
        "type": "article",
        "id": f"cmp-{cached.id}",
        "title": f"Сравнение #{cached.id}",
        "description": "Бабл пришлёт бот в личке: /cmp #номер",
        "input_message_content": {
            "message_text": (
                f"Сравнение #{cached.id} есть в кеше, но Telegram пока не вставляет "
                "HTML-баблы в inline-выдачу. Пришлите боту в личке "
                f"/cmp #{cached.id} — он отправит бабл сообщением."
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


def _html_bytes(result: dict) -> int:
    """Размер HTML-бабла в байтах UTF-8: Telegram считает лимит именно так."""
    content = result.get("input_message_content") or result
    html = content.get("html") or ""
    return len(html.encode("utf-8"))


def _context(api, query, text: str) -> str:
    """Что полезно знать про запрос, не раскрывая токен: контур, чат, отправитель."""
    return (
        f"'{text}' chat_type={getattr(query, 'chat_type', '-')} "
        f"user={getattr(getattr(query, 'from_user', None), 'id', '-')} "
        f"контур={getattr(api, 'base', '-')}"
    )


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
            log.info(
                "inline[%s]: сравнение #%s не найдено (%s, owner=%s, share_by_id=%s)",
                tag, digits, _context(api, query, text), owner_id,
                settings.cmp_share_by_id,
            )
            return True

        result = _found_result(cached)
        log.info(
            "inline[%s]: сравнение #%s из кеша (%s): html=%d байт, width=%d, quality=%d, zlib=%d, aspect=%s",
            tag, cached.id, _context(api, query, text), _html_bytes(result),
            cached.width, cached.quality, cached.zlib_bytes, cached.aspect,
        )
        if await _answer_widget(api, query, cached, tag):
            return True

        # Виджеты в inline-выдаче Telegram не пропустил — отдаём хотя бы ссылку на бабл.
        await _answer(api, query, [_refused_result(cached)])
        log.warning(
            "inline[%s]: бабл #%s не вставился (виджеты в inline запрещены), "
            "отдал текст с номером",
            tag, cached.id,
        )
        return True
    except api_module.WidgetAPIError as exc:
        # Протухший query ID — обычное дело; главное не улететь в DeepSeek.
        log.warning(
            "inline[%s]: Bot API отклонил %s: код=%s, описание=%s",
            tag, _context(api, query, text), exc.code, exc.description,
        )
        return True


__all__ = ["HINT_RE", "ID_RE", "try_answer"]
