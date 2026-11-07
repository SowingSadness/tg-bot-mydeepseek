"""Тесты клиента Bot API 10.4. Сеть не используется: транспорт внедряется явно."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from bot.compare import api as api_module
from bot.compare.api import WidgetAPI, WidgetAPIError, configure, get_api

TOKEN = "123:ABC"
BETA = "https://api.telegram.org/beta"
BASE_URL = f"{BETA}/bot{TOKEN}"


class FakeTransport:
    """Записывает вызовы и отдаёт заранее заданные `(status, json)`."""

    def __init__(self, *responses: tuple[int, object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url: str, payload: dict) -> tuple[int, object]:
        self.calls.append((url, payload))
        if not self.responses:
            raise AssertionError("транспорт вызван больше раз, чем задано ответов")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture(autouse=True)
def _clean_singleton(monkeypatch: pytest.MonkeyPatch):
    """Синглтон не должен протекать между тестами."""
    monkeypatch.setattr(api_module, "_api", None)
    yield


def _api(*responses, **kwargs) -> tuple[WidgetAPI, FakeTransport]:
    transport = FakeTransport(*responses)
    return WidgetAPI(TOKEN, transport=transport, **kwargs), transport


def test_url_base_variants():
    assert WidgetAPI(TOKEN).url("sendWidget") == f"{BASE_URL}/sendWidget"

    assert (
        WidgetAPI(TOKEN, test_env=True).url("sendWidget")
        == f"{BASE_URL}/test/sendWidget"
    )

    api = WidgetAPI(TOKEN, base="https://api.telegram.org/beta/")
    assert api.url("answerInlineQuery") == f"{BASE_URL}/answerInlineQuery"


async def test_send_widget_body():
    api, transport = _api((200, {"ok": True, "result": {"message_id": 1}}))

    result = await api.send_widget(
        42, "<html>", caption="Сравнение #482913 · До / После", width=430, height=320
    )

    assert result == {"message_id": 1}
    url, body = transport.calls[0]
    assert url == f"{BASE_URL}/sendWidget"
    assert body == {
        "chat_id": 42,
        "html": "<html>",
        "width": 430,
        "height": 320,
        "caption": "Сравнение #482913 · До / После",
    }
    assert "allow_queries" not in body
    assert "is_shared" not in body

    api, transport = _api((200, {"ok": True, "result": {}}))
    await api.send_widget(42, "<html>")
    _, body = transport.calls[0]
    assert "caption" not in body
    assert body == {"chat_id": 42, "html": "<html>", "width": 0, "height": 0}


async def test_answer_inline_passes_results_as_is_and_custom_cache_time():
    results = [
        {
            "type": "article",
            "id": "482913",
            "title": "Сравнение #482913",
            "input_message_content": {"html": "<html>", "width": 0, "height": 0},
        }
    ]
    api, transport = _api((200, {"ok": True, "result": True}))

    await api.answer_inline("query-1", results)

    url, body = transport.calls[0]
    assert url == f"{BASE_URL}/answerInlineQuery"
    assert body == {
        "inline_query_id": "query-1",
        "results": results,
        "cache_time": 0,
        "is_personal": True,
    }
    # Без pydantic-валидации и без копирования: тот же объект.
    assert body["results"] is results

    api, transport = _api((200, {"ok": True, "result": True}))
    await api.answer_inline("query-1", [], cache_time=300, is_personal=False)
    _, body = transport.calls[0]
    assert body["cache_time"] == 300
    assert body["is_personal"] is False


async def test_error_code_and_description():
    api, transport = _api(
        (400, {"ok": False, "error_code": 400, "description": "INPUT_MEDIA_WIDGET_TOO_LARGE"})
    )

    with pytest.raises(WidgetAPIError) as caught:
        await api.call("sendWidget", chat_id=1)

    error = caught.value
    assert error.code == 400
    assert error.description == "INPUT_MEDIA_WIDGET_TOO_LARGE"
    assert error.retry_after is None
    assert "400" in str(error)
    assert "INPUT_MEDIA_WIDGET_TOO_LARGE" in str(error)
    assert len(transport.calls) == 1


async def test_retry_after_retries_once_and_gives_up(monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    api, transport = _api(
        (
            429,
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 1",
                "parameters": {"retry_after": 0},
            },
        ),
        (200, {"ok": True, "result": {"message_id": 1}}),
    )

    result = await api.call("sendWidget", chat_id=1)

    assert result == {"message_id": 1}
    assert len(transport.calls) == 2
    assert sleeps == [0.0]

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    failure = (
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 1},
        },
    )
    api, transport = _api(failure, failure, failure)

    with pytest.raises(WidgetAPIError) as caught:
        await api.call("sendWidget", chat_id=1)

    assert caught.value.retry_after == 1.0
    assert len(transport.calls) == 3  # первый вызов + ровно 2 повтора


async def test_three_failures_without_retry_after_raise_immediately():
    failure = (400, {"ok": False, "error_code": 400, "description": "Bad Request"})
    api, transport = _api(failure, failure, failure)

    with pytest.raises(WidgetAPIError):
        await api.call("sendWidget", chat_id=1)

    assert len(transport.calls) == 1


async def test_failures_are_wrapped():
    """Не-JSON, пустое тело 500 и сетевая ошибка — всё в `WidgetAPIError`."""
    api, _ = _api((502, None))
    with pytest.raises(WidgetAPIError) as caught:
        await api.call("sendWidget", chat_id=1)
    assert caught.value.code is None
    assert "502" in caught.value.description

    api, _ = _api((500, {}))
    with pytest.raises(WidgetAPIError) as caught:
        await api.call("sendWidget", chat_id=1)
    assert caught.value.code is None
    assert "500" in caught.value.description

    api, _ = _api(aiohttp.ClientError("boom"))
    with pytest.raises(WidgetAPIError) as caught:
        await api.call("sendWidget", chat_id=1)
    assert caught.value.code is None
    assert "Bot API" in caught.value.description


def test_configure_and_get_api_singleton_and_missing():
    with pytest.raises(RuntimeError, match="configure"):
        get_api()

    api = WidgetAPI(TOKEN)
    configure(api)
    assert get_api() is api


async def test_session_is_lazy_and_closed_without_session(monkeypatch: pytest.MonkeyPatch):
    api = WidgetAPI(TOKEN)

    session = api.session
    assert api.session is session

    await api.close()
    assert api._session is None
    await api.close()
    assert api._session is None

    def forbidden(*args, **kwargs):
        raise AssertionError("aiohttp.ClientSession не должна создаваться")

    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    unused = WidgetAPI(TOKEN)

    assert unused._session is None
    await unused.close()
    await unused.close()
    assert unused._session is None
