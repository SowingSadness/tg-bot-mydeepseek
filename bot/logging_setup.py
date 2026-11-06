"""Настройка логирования и безопасные описания апдейтов и вызовов Bot API.

Модуль держится на стандартной библиотеке: тяжёлые зависимости и `bot.config`
импортируются только лениво, чтобы его можно было подключать где угодно.
Токен бота и длинные base64-картинки вырезаются `redact()`: они не должны
попадать в лог ни при каком уровне, включая `DEBUG`.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

#: Формат строк лога — тот же, что был у `logging.basicConfig` в `bot/main.py`.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: Предел длины произвольного текста в одной строке лога.
MAX_TEXT = 120

#: Токен в формате Bot API: `<id>:<35+ символов>`. Ловит и чужие токены.
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{25,}")

#: `data:image/...;base64,<payload>` — основа для сжатия вложений в логах.
_DATA_URI_RE = re.compile(
    r"data:(?P<mime>image/[\w.+-]+);base64,(?P<data>[A-Za-z0-9+/=]+)"
)

#: Короткие data-URI не трогаем, длинные — сжимаем до маркера.
MIN_DATA_URI = 64

#: Поля `Update`, по которым определяется тип апдейта (порядок важен).
_UPDATE_FIELDS: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "business_message",
    "edited_business_message",
    "inline_query",
    "chosen_inline_result",
    "callback_query",
    "shipping_query",
    "pre_checkout_query",
    "poll",
    "poll_answer",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
    "message_reaction",
    "message_reaction_count",
    "chat_boost",
    "removed_chat_boost",
    "purchased_paid_media",
)

#: Поля, в которых лежит `Message` (для chat_id/текста/медиа).
_MESSAGE_FIELDS: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "business_message",
    "edited_business_message",
)


# --------------------------------------------------------------------- настройка


def _configured_level() -> str | None:
    """Уровень из `Settings`; без настроек — `None` (вызывающий возьмёт `ERROR`)."""
    try:
        from .config import settings  # ленивый импорт: не тянем config на импорте
    except Exception:  # noqa: BLE001 — модуль должен импортироваться и без окружения
        return None
    level = getattr(settings, "log_level", None)
    return level if isinstance(level, str) and level else None


def setup_logging(level: str | None = None) -> None:
    """Настраивает корневой логгер: формат прежний, уровень — аргумент или настройки.

    По умолчанию в `Settings.log_level` стоит `ERROR`, поэтому в проде тихо;
    для отладки достаточно `LOG_LEVEL=DEBUG` в окружении / `.env`.
    """
    resolved = (level or _configured_level() or "ERROR").strip().upper()
    numeric = logging.getLevelName(resolved)
    logging.basicConfig(
        level=numeric if isinstance(numeric, int) else logging.ERROR,
        format=LOG_FORMAT,
    )


# --------------------------------------------------------------------- redact


def _configured_token() -> str:
    try:
        from .config import settings
    except Exception:  # noqa: BLE001
        return ""
    token = getattr(settings, "telegram_bot_token", "")
    return token.strip() if isinstance(token, str) else ""


def redact(text: str, *, token: str | None = None) -> str:
    """Убирает из строки токен бота и длинные base64-картинки.

    `token` позволяют передать явно (клиент Bot API знает свой токен); иначе
    берётся `settings.telegram_bot_token`. Пустой токен пропускается, чтобы
    `str.replace("")` не вставлял маркер между каждым символом.
    """
    if text is None:
        return ""
    value = str(text)
    secret = (token if token is not None else _configured_token()).strip()
    if secret:
        value = value.replace(secret, "<token>")
    value = _TOKEN_RE.sub("<token>", value)
    return _DATA_URI_RE.sub(_shrink_data_uri, value)


def _shrink_data_uri(match: re.Match[str]) -> str:
    payload = match.group("data")
    if len(payload) <= MIN_DATA_URI:
        return match.group(0)
    return f"data:{match.group('mime')};{len(payload)}chars"


# --------------------------------------------------------------------- helpers


def _one_line(value: Any, limit: int = MAX_TEXT) -> str:
    """Схлопывает пробелы/переводы строк и урезает длинный текст."""
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


def _enum_value(value: Any) -> Any:
    """`ChatType.PRIVATE` и `"private"` → `"private"`."""
    return getattr(value, "value", value)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:  # noqa: BLE001 — сломанный/частичный объект не должен ронять лог
        return default


def _first(obj: Any, names: Iterable[str]) -> Any:
    for name in names:
        value = _field(obj, name)
        if value is not None:
            return value
    return None


# --------------------------------------------------------------------- updates


def _update_kind(update: Any) -> str:
    """Тип апдейта: `message`, `callback_query`, … (или имя класса)."""
    kind = _field(update, "event_type")
    if isinstance(kind, str) and kind:
        return kind
    for name in _UPDATE_FIELDS:
        if _field(update, name) is not None:
            return name
    return type(update).__name__


def _update_message(update: Any) -> Any:
    message = _first(update, _MESSAGE_FIELDS)
    if message is not None:
        return message
    callback = _field(update, "callback_query")
    if callback is not None:
        return _field(callback, "message")
    return None


def _update_user(update: Any) -> Any:
    message = _update_message(update)
    if message is not None:
        user = _field(message, "from_user")
        if user is not None:
            return user
    for name in ("callback_query", "inline_query", "chosen_inline_result"):
        event = _field(update, name)
        if event is not None:
            user = _field(event, "from_user")
            if user is not None:
                return user
    for name in ("shipping_query", "pre_checkout_query"):
        event = _field(update, name)
        if event is not None:
            return event  # у этих апдейтов нет from_user, есть только id/данные
    return None


def _describe_media(message: Any) -> str:
    """`photo`/`document` с file_id и размером; для альбома — так же."""
    photos = _field(message, "photo")
    if isinstance(photos, (list, tuple)) and photos:
        photo = photos[-1]
        parts = [f"media=photo file_id={_field(photo, 'file_id', '?')}"]
        width, height = _field(photo, "width"), _field(photo, "height")
        if width and height:
            parts.append(f"size={width}x{height}")
        file_size = _field(photo, "file_size")
        if file_size:
            parts.append(f"bytes={file_size}")
        return " ".join(parts)

    document = _field(message, "document")
    if document is not None:
        parts = [f"media=document file_id={_field(document, 'file_id', '?')}"]
        name = _field(document, "file_name")
        if name:
            parts.append(f"name={_one_line(name, 64)!r}")
        mime = _field(document, "mime_type")
        if mime:
            parts.append(f"mime={mime}")
        file_size = _field(document, "file_size")
        if file_size:
            parts.append(f"bytes={file_size}")
        return " ".join(parts)
    return ""


def describe_update(update: Any) -> str:
    """Одна строка для DEBUG: тип, чат, пользователь, текст/подпись, медиа.

    Никогда не бросает исключений: любые неожиданные/частичные объекты дают
    строку с именем класса вместо падения обработки апдейта.
    """
    try:
        parts = [f"update={_update_kind(update)}"]

        message = _update_message(update)
        if message is not None:
            chat = _field(message, "chat")
            if chat is not None:
                parts.append(f"chat_id={_field(chat, 'id', '?')}")
                parts.append(f"chat_type={_enum_value(_field(chat, 'type', '?'))}")
            message_id = _field(message, "message_id")
            if message_id is not None:
                parts.append(f"message_id={message_id}")

        user = _update_user(update)
        if user is not None:
            user_id = _field(user, "id")
            if user_id is not None:
                parts.append(f"user_id={user_id}")
            username = _field(user, "username")
            if username:
                parts.append(f"username={username}")

        if message is not None:
            text = _field(message, "text")
            caption = _field(message, "caption")
            if text:
                parts.append(f"text={_one_line(redact(text))!r}")
            if caption:
                parts.append(f"caption={_one_line(redact(caption))!r}")
            media = _describe_media(message)
            if media:
                parts.append(media)
            media_group_id = _field(message, "media_group_id")
            if media_group_id:
                parts.append(f"media_group_id={media_group_id}")
        else:
            inline = _field(update, "inline_query")
            if inline is not None:
                query = _field(inline, "query")
                if query:
                    parts.append(f"query={_one_line(redact(query))!r}")
            callback = _field(update, "callback_query")
            if callback is not None:
                data = _field(callback, "data")
                if data:
                    parts.append(f"callback_data={_one_line(redact(data))!r}")

        return _one_line(redact(" ".join(parts)), limit=1000)
    except Exception:  # noqa: BLE001 — лог не имеет права ломать обработку
        return f"update={type(update).__name__} (не удалось разобрать)"


# --------------------------------------------------------------------- Bot API


def describe_api_call(
    method: str, params: dict | None = None, *, token: str | None = None
) -> str:
    """Одна строка о вызове метода: `html`/`results` — только размеры.

    Полный HTML и base64 никогда не печатаются: только `<N>chars`/`<N>`.
    """
    parts = [f"api {method}"]
    for key, value in (params or {}).items():
        if key == "html":
            parts.append(f"html={len(str(value))}chars")
        elif key == "results":
            try:
                parts.append(f"results={len(value)}")
            except TypeError:
                parts.append("results=?")
        elif isinstance(value, str):
            parts.append(f"{key}={_one_line(redact(value, token=token), 80)!r}")
        else:
            parts.append(f"{key}={_one_line(value, 80)}")
    return _one_line(redact(" ".join(parts), token=token), limit=1000)


def _describe_result(result: Any) -> str:
    """Краткий итог ответа: `message_id`, число результатов, `widget_id`."""
    if isinstance(result, (list, tuple)):
        return f"results={len(result)}"
    if isinstance(result, dict):
        parts: list[str] = []
        for key in ("message_id", "widget_id", "id"):
            value = result.get(key)
            if value is not None:
                parts.append(f"{key}={_one_line(value, 64)}")
        if parts:
            return " ".join(parts)
        return f"result_keys={len(result)}"
    if result is None:
        return "result=None"
    return f"result={_one_line(result, 64)}"


def describe_api_response(
    method: str, status: int, payload: Any, *, token: str | None = None
) -> str:
    """Одна строка об ответе Bot API: `ok`/`err`, код, описание, краткий результат."""
    parts = [f"api {method}", f"http={status}"]
    if not isinstance(payload, dict):
        parts.append("err")
        parts.append("description='ответ не JSON'")
        return _one_line(redact(" ".join(parts), token=token), limit=1000)

    ok = payload.get("ok") is True
    parts.append("ok" if ok else "err")
    if not ok:
        code = payload.get("error_code")
        if code is not None:
            parts.append(f"error_code={code}")
        description = payload.get("description")
        if description:
            parts.append(
                f"description={_one_line(redact(str(description), token=token))!r}"
            )
    else:
        parts.append(_describe_result(payload.get("result")))

    return _one_line(redact(" ".join(parts), token=token), limit=1000)


__all__ = [
    "LOG_FORMAT",
    "MAX_TEXT",
    "describe_api_call",
    "describe_api_response",
    "describe_update",
    "redact",
    "setup_logging",
]
