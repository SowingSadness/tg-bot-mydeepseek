"""Подпись `startapp`-токена и проверка `initData` мини-аппа (только stdlib).

Токен сравнения — `<id>_<20 симв. base64url без '='>`, HMAC-SHA256 от токена бота
и номера. Формат намеренно совпадает с `bot.compare.inline._start_token`:
Telegram кеширует мини-апп по ссылке, поэтому уже отправленные кнопки обязаны
работать и после переезда криптографии в этот модуль.

`initData` проверяется по алгоритму Bot API: `secret = HMAC_SHA256("WebAppData",
bot_token)`, затем `HMAC_SHA256(secret, data_check_string)` в hex сравнивается с
полем `hash`. Дополнительно проверяется свежесть `auth_date`.

Секреты (токен бота, `initData`) здесь не логируются никогда.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl

from ..config import settings

#: Длина подписи в символах base64url (без `=`), как в `inline._start_token`.
SIG_LEN = 20

#: Допустимый формат токена: `<цифры>_<base64url≥16>`.
TOKEN_RE = re.compile(r"^(\d{1,16})_([A-Za-z0-9_-]{16,})$")

#: Сколько живёт `initData` по умолчанию (сутки), если настройки ещё нет.
DEFAULT_AUTH_TTL = 86400

#: Ключ для вывода секрета из токена бота (по документации Bot API).
_WEBAPP_DATA_KEY = b"WebAppData"


def _signature(cmp_id: str) -> str:
    """20 символов base64url от `HMAC-SHA256(telegram_bot_token, cmp_id)`."""
    return (
        base64.urlsafe_b64encode(
            hmac.new(
                settings.telegram_bot_token.encode(), cmp_id.encode(), hashlib.sha256
            ).digest()
        )
        .decode()
        .rstrip("=")
    )[:SIG_LEN]


def sign(cmp_id: str) -> str:
    """Токен для `startapp`: `<id>_<подпись>` (≤ 64 символов, base64url).

    Результат байт-в-байт совпадает с `bot.compare.inline._start_token`,
    поэтому ссылки, уже отправленные в чат, продолжают открывать мини-апп.
    """
    return f"{cmp_id}_{_signature(cmp_id)}"


def verify(token: str) -> str | None:
    """Проверяет токен `startapp` и возвращает номер сравнения (или `None`).

    Никаких исключений наружу: битый, чужой, пустой или слишком длинный токен
    — это просто `None`. Подпись сверяется `hmac.compare_digest`.
    """
    try:
        if not isinstance(token, str) or len(token) > 64:
            return None
        match = TOKEN_RE.fullmatch(token)
        if match is None:
            return None
        cmp_id, sig = match.groups()
        if not hmac.compare_digest(_signature(cmp_id), sig):
            return None
        return cmp_id
    except Exception:  # noqa: BLE001 — проверка не должна падать наружу
        return None


def _auth_ttl(max_age: int | None) -> int:
    """Срок жизни `initData`: явный аргумент, иначе настройка, иначе сутки."""
    if max_age is not None:
        return int(max_age)
    return int(getattr(settings, "cmp_webapp_auth_ttl", DEFAULT_AUTH_TTL))


def parse_init_data(
    init_data: str, bot_token: str, *, max_age: int | None = None
) -> dict | None:
    """Проверяет `initData` из `Telegram.WebApp` и возвращает разобранные поля.

    Алгоритм Bot API: поля сортируются по имени, склеиваются через `\\n` как
    `key=value` (без `hash`), затем `secret = HMAC_SHA256(key="WebAppData",
    msg=bot_token)` и `HMAC_SHA256(key=secret, msg=data_check_string)` в hex
    сравнивается с `hash`. `auth_date` (unix ts) не должен быть старше
    `max_age` (по умолчанию `settings.cmp_webapp_auth_ttl`, иначе сутки).

    Возвращает `{"user": {...}, "auth_date": int, "query_id": str | None}`
    или `None` при любой ошибке (битый hash, отсутствие `auth_date`/`user`,
    просрочка, мусор). Исключения наружу не выходят, `initData` не логируется.
    """
    try:
        if not isinstance(init_data, str) or not init_data or not bot_token:
            return None
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
        if not pairs:
            return None
        received_hash: str | None = None
        fields: dict[str, str] = {}
        for key, value in pairs:
            if key == "hash":
                # Дубли поля `hash` — признак подделки, а не нормы.
                if received_hash is not None:
                    return None
                received_hash = value
                continue
            fields[key] = value
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
        secret = hmac.new(_WEBAPP_DATA_KEY, bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(
            secret, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, received_hash.lower()):
            return None

        raw_auth_date = fields.get("auth_date")
        if not raw_auth_date:
            return None
        auth_date = int(raw_auth_date)
        if auth_date <= 0:
            return None
        if int(time.time()) - auth_date > _auth_ttl(max_age):
            return None

        raw_user = fields.get("user")
        if not raw_user:
            return None
        user = json.loads(raw_user)
        if not isinstance(user, dict):
            return None

        return {
            "user": user,
            "auth_date": auth_date,
            "query_id": fields.get("query_id") or None,
        }
    except Exception:  # noqa: BLE001 — битый initData не должен ронять сервер
        return None


def owner_allowed(user_id: int | None, owner_id: int, share_by_id: bool) -> bool:
    """Можно ли `user_id` смотреть сравнение `owner_id`.

    При `share_by_id=True` номер сравнения работает как ссылка-доступ и
    владелец не проверяется; иначе смотреть может только автор сравнения.
    """
    if share_by_id:
        return True
    return user_id is not None and user_id == owner_id
