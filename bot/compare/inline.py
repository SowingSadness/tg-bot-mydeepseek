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

        await _answer(api, query, [_found_result(cached)])
        log.info(
            "inline[%s]: сравнение #%s из кеша (width=%d, quality=%d, zlib=%d)",
            tag, cached.id, cached.width, cached.quality, cached.zlib_bytes,
        )
        return True
    except api_module.WidgetAPIError as exc:
        # Протухший query ID — обычное дело; главное не улететь в DeepSeek.
        log.warning("inline[%s]: не удалось ответить на '%s': %s", tag, text, exc)
        return True


__all__ = ["HINT_RE", "ID_RE", "try_answer"]
