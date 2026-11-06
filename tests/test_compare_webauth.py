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

import pytest

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


# ------------------------------------------------------------------------ sign


def test_sign_matches_inline_start_token() -> None:
    """`webauth.sign` обязан давать байт-в-байт тот же токен, что inline."""
    for cmp_id in CMP_IDS:
        assert webauth.sign(cmp_id) == compare_inline._start_token(cmp_id)


def test_sign_format_is_short_base64url() -> None:
    """Токен ≤ 64 символов и состоит только из цифр, `_` и base64url."""
    for cmp_id in CMP_IDS:
        token = webauth.sign(cmp_id)
        assert len(token) <= 64
        assert webauth.TOKEN_RE.fullmatch(token) is not None
        assert token.startswith(f"{cmp_id}_")
        assert len(token) == len(cmp_id) + 1 + webauth.SIG_LEN


def test_sign_is_stable() -> None:
    """Один и тот же номер — один и тот же токен (Telegram кеширует ссылку)."""
    assert webauth.sign("482913") == webauth.sign("482913")


def test_sign_differs_for_different_ids() -> None:
    """Разные номера не должны давать одинаковую подпись."""
    signatures = {webauth.sign(cmp_id).split("_", 1)[1] for cmp_id in CMP_IDS}
    assert len(signatures) == len(CMP_IDS)


# ---------------------------------------------------------------------- verify


def test_verify_accepts_own_token() -> None:
    """Свой токен разбирается обратно в номер сравнения."""
    for cmp_id in CMP_IDS:
        assert webauth.verify(webauth.sign(cmp_id)) == cmp_id


def test_verify_rejects_broken_signature() -> None:
    """Испорченная подпись — None."""
    cmp_id = "482913"
    token = webauth.sign(cmp_id)
    sig = token.split("_", 1)[1]
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert webauth.verify(f"{cmp_id}_{flipped}") is None


def test_verify_rejects_other_bot_token() -> None:
    """Подпись чужим токеном бота (другой секрет) — None."""
    cmp_id = "482913"
    foreign_sig = (
        base64.urlsafe_b64encode(
            hmac.new(b"999:other-token", cmp_id.encode(), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")[: webauth.SIG_LEN]
    )
    assert foreign_sig != webauth.sign(cmp_id).split("_", 1)[1]
    assert webauth.verify(f"{cmp_id}_{foreign_sig}") is None


def test_verify_rejects_foreign_id_with_valid_signature() -> None:
    """Подпись от другого номера не подходит (номер входит в HMAC)."""
    token = webauth.sign("482913")
    sig = token.split("_", 1)[1]
    assert webauth.verify(f"820588_{sig}") is None


@pytest.mark.parametrize(
    "token",
    [
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
    ],
)
def test_verify_rejects_malformed(token: str) -> None:
    """Битые форматы — None, без исключений."""
    assert webauth.verify(token) is None


def test_verify_rejects_non_str_without_raising() -> None:
    """Не-строка (None, байты, число) — тоже просто None."""
    for value in (None, b"482913_0123456789abcdefghij", 12345):
        assert webauth.verify(value) is None  # type: ignore[arg-type]


# -------------------------------------------------------------- parse_init_data


def test_parse_init_data_ok() -> None:
    """Валидный `initData` разбирается в user/auth_date/query_id."""
    auth_date = int(time.time())
    parsed = webauth.parse_init_data(
        make_init_data(auth_date=auth_date), BOT_TOKEN
    )
    assert parsed is not None
    assert parsed["user"] == USER
    assert parsed["auth_date"] == auth_date
    assert parsed["query_id"] == "query-1"


def test_parse_init_data_without_query_id() -> None:
    """Без `query_id` поле есть, но пустое (None)."""
    parsed = webauth.parse_init_data(make_init_data(query_id=None), BOT_TOKEN)
    assert parsed is not None
    assert parsed["query_id"] is None


def test_parse_init_data_wrong_hash() -> None:
    """Неверный `hash` — None."""
    init_data = with_field(make_init_data(), hash="0" * 64)
    assert webauth.parse_init_data(init_data, BOT_TOKEN) is None


def test_parse_init_data_other_bot_token() -> None:
    """`initData` проверяется конкретным токеном бота."""
    assert (
        webauth.parse_init_data(make_init_data(), "999:other-token") is None
    )


def test_parse_init_data_expired() -> None:
    """`auth_date` старше TTL — None."""
    ttl = webauth._auth_ttl(None)
    old = int(time.time()) - ttl - 10
    assert webauth.parse_init_data(make_init_data(auth_date=old), BOT_TOKEN) is None


def test_parse_init_data_respects_explicit_max_age() -> None:
    """Явный `max_age` переопределяет настройку."""
    auth_date = int(time.time()) - 60
    init_data = make_init_data(auth_date=auth_date)
    assert webauth.parse_init_data(init_data, BOT_TOKEN, max_age=30) is None
    assert webauth.parse_init_data(init_data, BOT_TOKEN, max_age=3600) is not None


def test_parse_init_data_within_default_ttl() -> None:
    """Свежий `auth_date` проходит и без явного `max_age`."""
    auth_date = int(time.time()) - 30
    parsed = webauth.parse_init_data(make_init_data(auth_date=auth_date), BOT_TOKEN)
    assert parsed is not None
    assert parsed["auth_date"] == auth_date


def test_parse_init_data_missing_hash() -> None:
    """Нет `hash` — None."""
    init_data = make_init_data()
    without_hash = "&".join(
        pair for pair in init_data.split("&") if not pair.startswith("hash=")
    )
    assert webauth.parse_init_data(without_hash, BOT_TOKEN) is None


def test_parse_init_data_duplicate_hash() -> None:
    """Дубль `hash` — подозрительно, None."""
    init_data = make_init_data()
    assert webauth.parse_init_data(init_data + "&hash=" + "0" * 64, BOT_TOKEN) is None


def test_parse_init_data_missing_auth_date() -> None:
    """Нет `auth_date` — None."""
    fields = {"user": json.dumps(USER), "query_id": "query-1"}
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    assert webauth.parse_init_data(urlencode(fields), BOT_TOKEN) is None


def test_parse_init_data_non_numeric_auth_date() -> None:
    """Нечисловой `auth_date` — None."""
    init_data = make_init_data(extra={"auth_date": "вчера"})
    assert webauth.parse_init_data(init_data, BOT_TOKEN) is None


def test_parse_init_data_missing_user() -> None:
    """Нет `user` — None (без пользователя авторизовать нечего)."""
    fields = {"auth_date": str(int(time.time())), "query_id": "query-1"}
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    assert webauth.parse_init_data(urlencode(fields), BOT_TOKEN) is None


def test_parse_init_data_user_not_a_dict() -> None:
    """`user` не объект JSON — None."""
    init_data = make_init_data(user=None)
    pairs = [pair for pair in init_data.split("&") if not pair.startswith("user=")]
    fields = dict(pair.split("=", 1) for pair in pairs)
    fields["user"] = "[]"
    data_check_string = "\n".join(
        f"{key}={fields[key]}" for key in sorted(fields) if key != "hash"
    )
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    assert webauth.parse_init_data(urlencode(fields), BOT_TOKEN) is None


@pytest.mark.parametrize(
    "init_data",
    [
        "",
        "мусор",
        "a=b",
        "hash=&user={}",
        "hash=zzz&auth_date=abc&user={}",
        "&&&",
        "user=%7B%7D&auth_date=&hash=deadbeef",
    ],
)
def test_parse_init_data_garbage(init_data: str) -> None:
    """Мусор — None, без исключений."""
    assert webauth.parse_init_data(init_data, BOT_TOKEN) is None


def test_parse_init_data_without_bot_token() -> None:
    """Пустой токен бота — проверять нечем, None."""
    assert webauth.parse_init_data(make_init_data(), "") is None


# --------------------------------------------------------------- owner_allowed


def test_owner_allowed_owner() -> None:
    """Автор сравнения видит свой номер."""
    assert webauth.owner_allowed(7, 7, False) is True


def test_owner_allowed_stranger() -> None:
    """Чужой пользователь без `share_by_id` — отказ."""
    assert webauth.owner_allowed(8, 7, False) is False


def test_owner_allowed_no_user() -> None:
    """Без `user_id` доступа нет."""
    assert webauth.owner_allowed(None, 7, False) is False


def test_owner_allowed_shared_by_id() -> None:
    """При `share_by_id` номер работает как ссылка-доступ."""
    assert webauth.owner_allowed(8, 7, True) is True
    assert webauth.owner_allowed(None, 7, True) is True
