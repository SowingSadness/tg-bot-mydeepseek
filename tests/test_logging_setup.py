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
    redact,
    setup_logging,
)

DATE = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)


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
