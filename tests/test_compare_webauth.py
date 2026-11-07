"""Тесты подписи `startapp`-токена и проверки `initData` мини-аппа.

Валидный `initData` собирается здесь же по алгоритму Bot API: так проверка
проверяется независимой (тестовой) реализацией, а не самой собой.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from bot.compare import inline as compare_inline
from bot.compare import webauth
from bot.config import settings

BOT_TOKEN = settings.telegram_bot_token
CMP_IDS = ["482913", "820588", "123456", "99999999"]
USER = {"id": 7, "first_name": "Кир", "username": "kir"}


# --------------------------------------------------------------------- helpers


def make_init_data(
    *,
    bot_token: str = BOT_TOKEN,
    user: dict | None = None,
    auth_date: int | None = None,
    query_id: str | None = "query-1",
    extra: dict | None = None,
) -> str:
    """Собирает `initData` с правильным `hash` (алгоритм Bot API)."""
    fields: dict[str, str] = {
        "user": json.dumps(user if user is not None else USER, ensure_ascii=False),
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
    }
    if query_id is not None:
        fields["query_id"] = query_id
    if extra:
        fields.update({key: str(value) for key, value in extra.items()})

    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


def with_field(init_data: str, **replacements: str) -> str:
    """Пересобирает `initData` из пар с заменой отдельных полей (без пересчёта hash)."""
    pairs = [pair.split("=", 1) for pair in init_data.split("&")]
    result = []
    for key, value in pairs:
        result.append((key, replacements.get(key, value)))
    return urlencode(result)


def make_fields_init_data(**fields: str) -> str:
    """`initData` из произвольных полей с правильным `hash` (для «нет user»/«нет auth_date»)."""
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


# ------------------------------------------------------------------------ sign


def test_sign_is_stable_short_base64url_and_matches_inline() -> None:
    """Токен = `<id>_<sig>`: короткий base64url, стабильный и тот же, что в inline."""
    for cmp_id in CMP_IDS:
        token = webauth.sign(cmp_id)
        assert token == compare_inline._start_token(cmp_id), cmp_id
        assert len(token) <= 64, token
        assert webauth.TOKEN_RE.fullmatch(token) is not None, token
        assert token.startswith(f"{cmp_id}_"), token
        assert len(token) == len(cmp_id) + 1 + webauth.SIG_LEN, token
        # Один и тот же номер — один и тот же токен (Telegram кеширует ссылку).
        assert webauth.sign(cmp_id) == token, cmp_id

    # Разные номера не должны давать одинаковую подпись.
    signatures = {webauth.sign(cmp_id).split("_", 1)[1] for cmp_id in CMP_IDS}
    assert len(signatures) == len(CMP_IDS)


# ---------------------------------------------------------------------- verify


def test_verify_accepts_own_token() -> None:
    """Свой токен разбирается обратно в номер сравнения."""
    for cmp_id in CMP_IDS:
        assert webauth.verify(webauth.sign(cmp_id)) == cmp_id


def test_verify_rejects_broken_foreign_and_malformed_tokens() -> None:
    """Подпись проверяется по секрету бота и по номеру; битые форматы — None."""
    cmp_id = "482913"
    token = webauth.sign(cmp_id)

    # Испорченная подпись.
    sig = token.split("_", 1)[1]
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert webauth.verify(f"{cmp_id}_{flipped}") is None

    # Подпись чужим токеном бота (другой секрет).
    foreign_sig = (
        base64.urlsafe_b64encode(
            hmac.new(b"999:other-token", cmp_id.encode(), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")[: webauth.SIG_LEN]
    )
    assert foreign_sig != sig
    assert webauth.verify(f"{cmp_id}_{foreign_sig}") is None

    # Подпись от другого номера не подходит (номер входит в HMAC).
    assert webauth.verify(f"820588_{sig}") is None

    malformed = [
        "",
        "482913",
        "482913_",
        "_0123456789abcdefghij",
        "482913_short",
        "abc_0123456789abcdefghij",
        "482913_0123456789abcde",
        "482913_0123456789abcdefghij.",
        "482913_0123456789abcdefghij extra",
        "482913_0123456789abcdefghi=",
        "482913_0123456789abcdefghij" + "0",
        "1" * 65 + "_0123456789abcdefghij",
    ]
    for broken in malformed:
        assert webauth.verify(broken) is None, broken


def test_verify_rejects_non_str_without_raising() -> None:
    """Не-строка (None, байты, число) — тоже просто None."""
    for value in (None, b"482913_0123456789abcdefghij", 12345):
        assert webauth.verify(value) is None  # type: ignore[arg-type]


# -------------------------------------------------------------- parse_init_data


def test_parse_init_data_ok() -> None:
    """Валидный `initData` разбирается в user/auth_date/query_id."""
    auth_date = int(time.time())
    parsed = webauth.parse_init_data(make_init_data(auth_date=auth_date), BOT_TOKEN)
    assert parsed is not None
    assert parsed["user"] == USER
    assert parsed["auth_date"] == auth_date
    assert parsed["query_id"] == "query-1"

    # Без `query_id` поле есть, но пустое (None).
    without_query = webauth.parse_init_data(make_init_data(query_id=None), BOT_TOKEN)
    assert without_query is not None
    assert without_query["query_id"] is None

    # Свежий `auth_date` проходит и без явного `max_age`.
    recent = int(time.time()) - 30
    fresh = webauth.parse_init_data(make_init_data(auth_date=recent), BOT_TOKEN)
    assert fresh is not None
    assert fresh["auth_date"] == recent


def test_parse_init_data_rejects_bad_hash_or_token() -> None:
    """Hash сверяется с токеном бота; дубль/отсутствие `hash` — отказ."""
    init_data = make_init_data()
    assert webauth.parse_init_data(with_field(init_data, hash="0" * 64), BOT_TOKEN) is None
    assert webauth.parse_init_data(init_data, "999:other-token") is None
    assert webauth.parse_init_data(init_data, "") is None

    without_hash = "&".join(
        pair for pair in init_data.split("&") if not pair.startswith("hash=")
    )
    assert webauth.parse_init_data(without_hash, BOT_TOKEN) is None
    # Дубль `hash` — подозрительно.
    assert webauth.parse_init_data(init_data + "&hash=" + "0" * 64, BOT_TOKEN) is None


def test_parse_init_data_enforces_ttl() -> None:
    """`auth_date` старше TTL — отказ; явный `max_age` переопределяет настройку."""
    ttl = webauth._auth_ttl(None)
    old = int(time.time()) - ttl - 10
    assert webauth.parse_init_data(make_init_data(auth_date=old), BOT_TOKEN) is None

    auth_date = int(time.time()) - 60
    init_data = make_init_data(auth_date=auth_date)
    assert webauth.parse_init_data(init_data, BOT_TOKEN, max_age=30) is None
    assert webauth.parse_init_data(init_data, BOT_TOKEN, max_age=3600) is not None


def test_parse_init_data_requires_valid_user_and_auth_date() -> None:
    """Без `user`/`auth_date` или с мусором в полях авторизовать нечего."""
    now = str(int(time.time()))
    # Нет `auth_date`.
    no_auth_date = make_fields_init_data(user=json.dumps(USER), query_id="query-1")
    assert webauth.parse_init_data(no_auth_date, BOT_TOKEN) is None
    # Нечисловой `auth_date`.
    assert (
        webauth.parse_init_data(
            make_init_data(extra={"auth_date": "вчера"}), BOT_TOKEN
        )
        is None
    )
    # Нет `user`.
    no_user = make_fields_init_data(auth_date=now, query_id="query-1")
    assert webauth.parse_init_data(no_user, BOT_TOKEN) is None
    # `user` не объект JSON.
    not_a_dict = make_init_data(user=None)
    pairs = [
        pair
        for pair in not_a_dict.split("&")
        if not pair.startswith(("user=", "hash="))
    ]
    fields = dict(pair.split("=", 1) for pair in pairs)
    fields["user"] = "[]"
    assert webauth.parse_init_data(make_fields_init_data(**fields), BOT_TOKEN) is None

    # Мусор — None, без исключений.
    for garbage in (
        "",
        "мусор",
        "a=b",
        "hash=&user={}",
        "hash=zzz&auth_date=abc&user={}",
        "&&&",
        "user=%7B%7D&auth_date=&hash=deadbeef",
    ):
        assert webauth.parse_init_data(garbage, BOT_TOKEN) is None, garbage


# --------------------------------------------------------------- owner_allowed


def test_owner_allowed_rules() -> None:
    """Автор видит свой номер; чужой — только при `share_by_id`."""
    assert webauth.owner_allowed(7, 7, False) is True
    assert webauth.owner_allowed(8, 7, False) is False
    assert webauth.owner_allowed(None, 7, False) is False
    # При `share_by_id` номер работает как ссылка-доступ, в том числе без user.
    assert webauth.owner_allowed(8, 7, True) is True
    assert webauth.owner_allowed(None, 7, True) is True
