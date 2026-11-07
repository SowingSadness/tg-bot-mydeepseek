"""Тесты модуля логов: токен/base64 вырезаются, описания — в одну строку."""

from __future__ import annotations

import datetime
import logging
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, Document, Message, PhotoSize, Update, User

from bot.compare.api import WidgetAPI
from bot.config import settings
from bot.logging_setup import (
    describe_api_call,
    describe_api_response,
    describe_update,
    describe_webapp_request,
    redact,
    redact_request_url,
    redact_url,
    setup_logging,
)

DATE = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)

#: Токен `startapp` в том виде, в каком он бывает в URL мини-аппа.
STARTAPP_TOKEN = "482913_AbC-123_x"


# --------------------------------------------------------------------- helpers


def make_message(**overrides) -> Message:
    base = {
        "message_id": 10,
        "date": DATE,
        "chat": Chat(id=777, type="private"),
        "from_user": User(id=42, is_bot=False, first_name="Test", username="tester"),
    }
    base.update(overrides)
    return Message(**base)


def make_update(message: Message | None = None) -> Update:
    return Update(update_id=1, message=message or make_message())


# ---------------------------------------------------------- setup / redact


def test_setup_logging_sets_root_level() -> None:
    """`setup_logging("DEBUG")` ставит уровень root-логгера и не портит соседние тесты.

    Хендлеры снимаются прямо в тесте (а не в фикстуре): pytest добавляет свои
    root-хендлеры уже после setup, а `logging.basicConfig` по контракту ничего не
    перенастраивает, если хендлеры есть. Так тест повторяет реальный старт бота
    (чистый root) и возвращает прежние хендлеры и уровень в `finally`.
    """
    assert callable(setup_logging)

    root = logging.getLogger()
    original_level = root.level
    original_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        setup_logging("DEBUG")

        assert root.level == logging.DEBUG
    finally:
        for handler in root.handlers[:]:
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert logging.getLogger().level == original_level


async def test_widget_api_debug_log_hides_html_and_token(caplog) -> None:
    token = "123:secret-token-value"
    html = '<html><img src="data:image/webp;base64,' + "A" * 4000 + '"></html>'

    async def transport(url: str, payload: dict) -> tuple[int, dict]:
        return 200, {"ok": True, "result": {"message_id": 7, "widget_id": "w1"}}

    api = WidgetAPI(token, transport=transport)
    with caplog.at_level(logging.DEBUG):
        await api.send_widget(777, html, caption="Сравнение #123456")

    logged = caplog.text
    assert "api sendWidget" in logged
    assert "chat_id=777" in logged
    assert f"html={len(html)}chars" in logged
    assert "base64" not in logged
    assert "A" * 50 not in logged
    assert token not in logged


def test_redact_replaces_tokens_and_shrinks_data_uri() -> None:
    """Токен бота (свой и чужой) маскируется; длинный base64 схлопывается до `Nchars`."""
    token = settings.telegram_bot_token
    text = f"POST https://api.telegram.org/bot{token}/sendWidget"
    result = redact(text)
    assert token not in result
    assert result == "POST https://api.telegram.org/bot<token>/sendWidget"

    foreign = "123456789:" + "A" * 35
    assert foreign not in redact(f"url=/bot{foreign}/getMe")

    uri = "data:image/webp;base64," + "A" * 1000
    shrunk = redact(uri)
    assert shrunk == "data:image/webp;1000chars"
    assert "AAAA" not in shrunk

    short = "data:image/png;base64," + "A" * 20
    assert redact(short) == short
    assert redact("") == ""


# --------------------------------------------------------------------- redact_url


def test_redact_url_masks_sensitive_parts_and_keeps_the_rest() -> None:
    """Прячутся только токен/mini-app данные; прочие параметры и порядок не тронуты."""
    result = redact_url(f"/app/cmp/img/{STARTAPP_TOKEN}/b?v=1")
    assert result == "/app/cmp/img/<token>/b?v=1"
    assert STARTAPP_TOKEN not in result

    result = redact_url(f"/api/cmp/{STARTAPP_TOKEN}?init_data=user%3D1%26hash%3Dx")
    assert STARTAPP_TOKEN not in result
    assert "user%3D1" not in result
    assert "init_data=<redacted>" in result

    result = redact_url(f"/app/cmp?t={STARTAPP_TOKEN}&v=2&startapp={STARTAPP_TOKEN}#hash=1")
    assert STARTAPP_TOKEN not in result
    assert "t=<redacted>" in result
    assert "startapp=<redacted>" in result
    assert "hash=<redacted>" in result
    assert result.startswith("/app/cmp?t=<redacted>&v=2&startapp=<redacted>")

    token = "337326_AbCdEfGhIjKlMnOpQrSt"
    result = redact_url(f"/app/cmp?tgWebAppStartParam={token}")
    assert token not in result
    assert result == "/app/cmp?tgWebAppStartParam=<redacted>"

    # Имя параметра сравнивается без учёта регистра.
    result = redact_url(f"https://t.me/cmpbot?V=1&tgwebappstartparam={token}&v=2")
    assert token not in result
    assert result.startswith("https://t.me/cmpbot?V=1&tgwebappstartparam=<redacted>")
    assert result.endswith("&v=2")

    result = redact_url("http://h/app/cmp#tgWebAppData=query_id%3D1&tgWebAppVersion=7")
    assert "query_id" not in result
    assert "tgWebAppData=<redacted>" in result
    assert "tgWebAppVersion=7" in result

    assert redact_url("обычный текст") == "обычный текст"
    assert redact_url("") == ""
    assert redact_url(None) == ""
    for value in (None, "", "/", "?", "/%%%", 42, object()):
        assert isinstance(redact_url(value), str)


# --------------------------------------------------------------------- webapp


def test_describe_webapp_request_format_and_token_visibility() -> None:
    """Строка `<status> <METHOD> <url> — <N> мс[ user=]`: токен виден, `webapp` — нет."""
    result = describe_webapp_request(
        "GET", "/api/cmp/337326_AbCdEfGhIjKlMnOpQrSt", 200, 1.4
    )
    assert result == "200 GET /api/cmp/337326_AbCdEfGhIjKlMnOpQrSt — 1 мс"
    assert "\u2014" in result  # длинное тире, а не дефис
    assert "webapp" not in result
    assert "ms" not in result
    assert "\n" not in result

    result = describe_webapp_request("GET", f"/api/cmp/{STARTAPP_TOKEN}", 200, 12.4)
    assert STARTAPP_TOKEN in result
    assert "<token>" not in result
    assert result == f"200 GET /api/cmp/{STARTAPP_TOKEN} — 12 мс"

    token = "337326_AbCdEfGhIjKlMnOpQrSt"
    result = describe_webapp_request(
        "GET", f"/app/cmp?tgWebAppStartParam={token}", 200, 1.4
    )
    assert result == f"200 GET /app/cmp?tgWebAppStartParam={token} — 1 мс"

    path = "/api/cmp/" + "a" * 200
    result = describe_webapp_request("GET", path, 200, 1.4)
    assert path in result
    assert "…" not in result

    result = describe_webapp_request(
        "GET", f"/api/cmp/{STARTAPP_TOKEN}", 200, 1.4, user_id=42
    )
    assert result == f"200 GET /api/cmp/{STARTAPP_TOKEN} — 1 мс user=42"
    assert result.endswith(" user=42")

    result = describe_webapp_request("POST", "/api/cmp/log", 204, 0.2)
    assert result == "204 POST /api/cmp/log — 0 мс"
    assert "user=" not in result

    result = describe_webapp_request("GET", "/api/cmp/<token>", 200, 12.4, user_id=42)
    assert "/api/cmp/<token>" in result

    for method, path, status, ms in (
        (None, None, None, None),
        (42, object(), 200, -5.0),
        ("get", 12345, "200", "1.4"),
    ):
        result = describe_webapp_request(method, path, status, ms)
        assert isinstance(result, str)
        assert "\n" not in result


def test_describe_webapp_request_hides_user_data_only() -> None:
    """`init_data`/`tgWebAppData` скрыты, а `tgWebAppStartParam` намеренно виден."""
    token = "337326_AbCdEfGhIjKlMnOpQrSt"
    result = describe_webapp_request(
        "GET",
        f"/app/cmp?init_data=user%3D1%26hash%3Dx&tgWebAppStartParam={token}&v=2",
        200,
        1.4,
    )
    assert "init_data=<скрыто>" in result
    assert "user%3D1" not in result
    assert f"tgWebAppStartParam={token}" in result
    assert "&v=2" in result
    assert result.endswith("— 1 мс")

    result = describe_webapp_request(
        "GET", "/app/cmp?tgWebAppData=query_id%3D1&tgWebAppVersion=7", 200, 1.4
    )
    assert "tgWebAppData=<скрыто>" in result
    assert "query_id" not in result
    assert "tgWebAppVersion=7" in result

    # Тот же параметр в `redact_url` маскируется, но в строке запроса остаётся.
    url = f"/app/cmp?tgWebAppStartParam={token}"
    assert redact_url(url) == "/app/cmp?tgWebAppStartParam=<redacted>"
    assert token not in redact_url(url)
    line = describe_webapp_request("GET", url, 200, 1)
    assert line == f"200 GET /app/cmp?tgWebAppStartParam={token} — 1 мс"
    assert token in line
    assert "<redacted>" not in line


def test_redact_request_url_hides_only_user_data() -> None:
    token = "337326_AbCdEfGhIjKlMnOpQrSt"
    result = redact_request_url(
        f"/app/cmp?init_data=user%3D1&tgWebAppStartParam={token}"
        "#tgWebAppData=query%3D1"
    )
    assert "init_data=<скрыто>" in result
    assert "tgWebAppData=<скрыто>" in result
    assert f"tgWebAppStartParam={token}" in result
    assert "user%3D1" not in result

    for value in (None, "", "/", "?", "/%%%", 42, object()):
        assert isinstance(redact_request_url(value), str)


# --------------------------------------------------------------------- updates


def test_describe_update_text_is_one_line() -> None:
    result = describe_update(make_update(make_message(text="привет, мир")))
    assert "update=message" in result
    assert "chat_id=777" in result
    assert "chat_type=private" in result
    assert "user_id=42" in result
    assert "username=tester" in result
    assert "text='привет, мир'" in result
    assert "\n" not in result


def test_describe_update_media_compact_and_verbose() -> None:
    """Компактный лог скрывает `file_id`/размеры, verbose — показывает их."""
    photo = PhotoSize(
        file_id="file-photo",
        file_unique_id="unique-photo",
        width=100,
        height=80,
        file_size=12345,
    )
    photo_message = make_message(
        photo=[photo], caption="До / После", media_group_id="album-1"
    )
    compact = describe_update(make_update(photo_message))
    assert "media=photo" in compact
    assert "caption='До / После'" in compact
    assert "file_id" not in compact
    assert "size=" not in compact
    assert "bytes=" not in compact
    assert "media_group_id" not in compact
    assert "\n" not in compact

    verbose = describe_update(make_update(photo_message), verbose=True)
    assert "media=photo" in verbose
    assert "file_id=file-photo" in verbose
    assert "size=100x80" in verbose
    assert "bytes=12345" in verbose
    assert "caption='До / После'" in verbose
    assert "media_group_id=album-1" in verbose

    document = Document(
        file_id="file-doc",
        file_unique_id="unique-doc",
        file_name="before.png",
        mime_type="image/png",
        file_size=2048,
    )
    update = make_update(make_message(document=document))
    compact = describe_update(update)
    assert "media=document" in compact
    assert "name='before.png'" in compact
    assert "file_id" not in compact
    assert "bytes=" not in compact

    verbose = describe_update(update, verbose=True)
    assert "media=document" in verbose
    assert "file_id=file-doc" in verbose
    assert "name='before.png'" in verbose
    assert "bytes=2048" in verbose


def test_describe_update_hides_bot_and_startapp_tokens() -> None:
    token = settings.telegram_bot_token
    link = f"https://t.me/bot?startapp={STARTAPP_TOKEN}"
    update = make_update(make_message(text=f"токен {token} и ссылка {link}"))
    for verbose in (False, True):
        result = describe_update(update, verbose=verbose)
        assert token not in result
        assert STARTAPP_TOKEN not in result
        assert "<token>" in result
        assert "startapp=<redacted>" in result


def test_describe_update_never_raises() -> None:
    for value in (None, SimpleNamespace(), SimpleNamespace(event_type="message")):
        assert isinstance(describe_update(value), str)
        assert isinstance(describe_update(value, verbose=True), str)


# --------------------------------------------------------------------- Bot API


def test_describe_api_call_hides_html_and_counts() -> None:
    html = "<html>" + "x" * 5000
    result = describe_api_call(
        "sendWidget",
        {
            "chat_id": 777,
            "html": html,
            "width": 430,
            "height": 286,
            "caption": f"token {settings.telegram_bot_token}",
        },
    )
    assert "api sendWidget" in result
    assert "chat_id=777" in result
    assert "html=5006chars" in result
    assert "x" * 50 not in result
    assert settings.telegram_bot_token not in result

    result = describe_api_call(
        "answerInlineQuery",
        {"inline_query_id": "q-1", "results": [{"type": "article"}] * 3},
    )
    assert "results=3" in result
    assert "\n" not in describe_api_call("getMe", {})


def test_describe_api_response_ok_error_and_not_json() -> None:
    result = describe_api_response(
        "sendWidget", 200, {"ok": True, "result": {"message_id": 42, "widget_id": "w1"}}
    )
    assert "api sendWidget" in result
    assert "http=200" in result
    assert "ok" in result
    assert "message_id=42" in result
    assert "widget_id=w1" in result

    result = describe_api_response(
        "answerInlineQuery", 200, {"ok": True, "result": ["a", "b"]}
    )
    assert "ok" in result
    assert "results=2" in result

    result = describe_api_response(
        "sendWidget",
        400,
        {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
    )
    assert "http=400" in result
    assert "err" in result
    assert "error_code=400" in result
    assert "Bad Request: chat not found" in result

    result = describe_api_response("getMe", 502, None)
    assert "err" in result
    assert "http=502" in result
