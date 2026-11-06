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


# --------------------------------------------------------------------- redact


def test_setup_logging_is_available() -> None:
    assert callable(setup_logging)


def test_setup_logging_sets_root_level() -> None:
    """`setup_logging("DEBUG")` ставит уровень root-логгера и не портит соседние тесты.

    Хендлеры снимаются прямо в тесте (а не в фикстуре): pytest добавляет свои
    root-хендлеры уже после setup, а `logging.basicConfig` по контракту ничего не
    перенастраивает, если хендлеры есть. Так тест повторяет реальный старт бота
    (чистый root) и возвращает прежние хендлеры и уровень в `finally`.
    """
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


def test_redact_replaces_configured_token() -> None:
    token = settings.telegram_bot_token
    text = f"POST https://api.telegram.org/bot{token}/sendWidget"
    result = redact(text)
    assert token not in result
    assert result == "POST https://api.telegram.org/bot<token>/sendWidget"


def test_redact_replaces_foreign_bot_token() -> None:
    foreign = "123456789:" + "A" * 35
    assert foreign not in redact(f"url=/bot{foreign}/getMe")


def test_redact_shrinks_long_data_uri() -> None:
    uri = "data:image/webp;base64," + "A" * 1000
    result = redact(uri)
    assert result == "data:image/webp;1000chars"
    assert "AAAA" not in result


def test_redact_keeps_short_data_uri() -> None:
    uri = "data:image/png;base64," + "A" * 20
    assert redact(uri) == uri


def test_redact_handles_empty_value() -> None:
    assert redact("") == ""


# --------------------------------------------------------------------- redact_url


def test_redact_url_masks_image_token() -> None:
    result = redact_url(f"/app/cmp/img/{STARTAPP_TOKEN}/b?v=1")
    assert result == "/app/cmp/img/<token>/b?v=1"
    assert STARTAPP_TOKEN not in result


def test_redact_url_masks_init_data_and_path_token() -> None:
    result = redact_url(
        f"/api/cmp/{STARTAPP_TOKEN}?init_data=user%3D1%26hash%3Dx"
    )
    assert STARTAPP_TOKEN not in result
    assert "user%3D1" not in result
    assert "init_data=<redacted>" in result


def test_redact_url_masks_sensitive_query_keys() -> None:
    result = redact_url(
        f"/app/cmp?t={STARTAPP_TOKEN}&v=2&startapp={STARTAPP_TOKEN}#hash=1"
    )
    assert STARTAPP_TOKEN not in result
    assert "t=<redacted>" in result
    assert "startapp=<redacted>" in result
    assert "hash=<redacted>" in result
    # остальные параметры и порядок не тронуты
    assert result.startswith("/app/cmp?t=<redacted>&v=2&startapp=<redacted>")


def test_redact_url_masks_fragment_webapp_data() -> None:
    result = redact_url("http://h/app/cmp#tgWebAppData=query_id%3D1&tgWebAppVersion=7")
    assert "query_id" not in result
    assert "tgWebAppData=<redacted>" in result
    assert "tgWebAppVersion=7" in result


def test_redact_url_keeps_plain_text() -> None:
    assert redact_url("обычный текст") == "обычный текст"
    assert redact_url("") == ""


def test_redact_url_handles_none() -> None:
    assert redact_url(None) == ""


def test_redact_url_never_raises() -> None:
    for value in (None, "", "/", "?", "/%%%", 42, object()):
        assert isinstance(redact_url(value), str)


# --------------------------------------------------------------------- webapp


def test_describe_webapp_request_has_all_parts() -> None:
    result = describe_webapp_request(
        "GET", f"/api/cmp/{STARTAPP_TOKEN}", 200, 12.4, user_id=42
    )
    assert result.startswith("webapp GET "), result
    assert "200" in result
    assert "12ms" in result
    assert "user=42" in result
    assert STARTAPP_TOKEN not in result
    assert "\n" not in result


def test_describe_webapp_request_without_user() -> None:
    result = describe_webapp_request("POST", "/api/cmp/log", 403, 0.2)
    assert "user=" not in result
    assert "0ms" in result


def test_describe_webapp_request_keeps_placeholder_path() -> None:
    result = describe_webapp_request("GET", "/api/cmp/<token>", 200, 12.4, user_id=42)
    assert "/api/cmp/<token>" in result


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


def test_describe_update_photo_album() -> None:
    photo = PhotoSize(
        file_id="file-photo",
        file_unique_id="unique-photo",
        width=100,
        height=80,
        file_size=12345,
    )
    message = make_message(
        photo=[photo], caption="До / После", media_group_id="album-1"
    )
    result = describe_update(make_update(message))
    assert "media=photo" in result
    assert "file_id=file-photo" in result
    assert "size=100x80" in result
    assert "bytes=12345" in result
    assert "caption='До / После'" in result
    assert "media_group_id=album-1" in result


def test_describe_update_document() -> None:
    document = Document(
        file_id="file-doc",
        file_unique_id="unique-doc",
        file_name="before.png",
        mime_type="image/png",
        file_size=2048,
    )
    result = describe_update(make_update(make_message(document=document)))
    assert "media=document" in result
    assert "file_id=file-doc" in result
    assert "name='before.png'" in result
    assert "bytes=2048" in result


def test_describe_update_never_raises() -> None:
    for value in (None, SimpleNamespace(), SimpleNamespace(event_type="message")):
        assert isinstance(describe_update(value), str)


# --------------------------------------------------------------------- Bot API


def test_describe_api_call_hides_html_and_truncates() -> None:
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


def test_describe_api_call_counts_results() -> None:
    result = describe_api_call(
        "answerInlineQuery",
        {"inline_query_id": "q-1", "results": [{"type": "article"}] * 3},
    )
    assert "results=3" in result


def test_describe_api_call_is_one_line() -> None:
    assert "\n" not in describe_api_call("getMe", {})


def test_describe_api_response_ok() -> None:
    result = describe_api_response(
        "sendWidget", 200, {"ok": True, "result": {"message_id": 42, "widget_id": "w1"}}
    )
    assert "api sendWidget" in result
    assert "http=200" in result
    assert "ok" in result
    assert "message_id=42" in result
    assert "widget_id=w1" in result


def test_describe_api_response_ok_with_list_result() -> None:
    result = describe_api_response(
        "answerInlineQuery", 200, {"ok": True, "result": ["a", "b"]}
    )
    assert "ok" in result
    assert "results=2" in result


def test_describe_api_response_error() -> None:
    result = describe_api_response(
        "sendWidget",
        400,
        {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
    )
    assert "http=400" in result
    assert "err" in result
    assert "error_code=400" in result
    assert "Bad Request: chat not found" in result


def test_describe_api_response_not_json() -> None:
    result = describe_api_response("getMe", 502, None)
    assert "err" in result
    assert "http=502" in result
