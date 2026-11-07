"""Тесты inline-ветки ``#номер``: без сети, кеш и Bot API — лёгкие фейки."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.compare import api as api_module
from bot.compare import inline as compare_inline
from bot.compare import service
from bot.compare.api import WidgetAPIError
from bot.compare.store import Cached
from bot.config import settings

USER_ID = 7
OTHER_USER_ID = 8
CMP_ID = "482913"
QUERY_ID = "query-1"
IMAGE_A = b"RIFF-a-webp-bytes"
IMAGE_B = b"RIFF-b-webp-bytes"
PREVIEW_FILE_ID = "AgACAgIAAxkBAA-preview-file-id"
SHORT_NAME = "cmp"

#: ``startapp`` = ``<id>_<подпись>``: только base64url (``A-Za-z0-9_-``).
START_TOKEN_RE = re.compile(r"[0-9]+_[A-Za-z0-9_-]{16,}")

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- helpers


def cached(
    cmp_id: str = CMP_ID,
    owner_id: int = USER_ID,
    *,
    preview_file_id: str | None = None,
) -> Cached:
    """Сравнение, как его отдаёт `ComparisonStore.get`."""
    return Cached(
        id=cmp_id,
        owner_id=owner_id,
        created_at=0.0,
        width=430,
        quality=34,
        labels=("До", "После"),
        zlib_bytes=1024,
        image_a=IMAGE_A,
        image_b=IMAGE_B,
        preview_file_id=preview_file_id,
    )


class FakeStore:
    """Кеш с `get(cmp_id, *, owner_id=None)`, как у `ComparisonStore`."""

    def __init__(self, *items: Cached) -> None:
        self.items = {item.id: item for item in items}
        self.calls: list[tuple[str, int | None]] = []

    async def get(self, cmp_id: str, *, owner_id: int | None = None) -> Cached | None:
        self.calls.append((cmp_id, owner_id))
        item = self.items.get(cmp_id)
        if item is None:
            return None
        if owner_id is not None and item.owner_id != owner_id:
            return None
        return item


class FakeAPI:
    """Bot API: записывает аргументы `answer_inline`, умеет бросить ошибку."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    async def answer_inline(
        self,
        query_id: str,
        results: list[dict],
        *,
        cache_time: int = 0,
        is_personal: bool = True,
    ) -> None:
        self.calls.append(
            {
                "query_id": query_id,
                "results": results,
                "cache_time": cache_time,
                "is_personal": is_personal,
            }
        )
        if self.error is not None:
            raise self.error


def make_query(text: str, user_id: int = USER_ID) -> SimpleNamespace:
    """Минимальный `InlineQuery`: id, query и from_user.id."""
    return SimpleNamespace(
        id=QUERY_ID,
        query=text,
        from_user=SimpleNamespace(id=user_id),
    )


def configure(store: FakeStore | None = None, api: FakeAPI | None = None) -> None:
    if store is not None:
        service.configure_store(store)
    if api is not None:
        api_module.configure(api)


@pytest.fixture(autouse=True)
def _clean_singletons(monkeypatch: pytest.MonkeyPatch):
    """Синглтоны, режим доступа и мини-апп не протекают между тестами."""
    monkeypatch.setattr(service, "_store", None)
    monkeypatch.setattr(api_module, "_api", None)
    monkeypatch.setattr(settings, "cmp_share_by_id", False)
    monkeypatch.setattr(settings, "cmp_webapp_short_name", "")
    monkeypatch.setattr(settings, "cmp_inline_widget", False)
    monkeypatch.setattr(settings, "bot_username", "test_bot")
    yield


def _article(api: FakeAPI) -> dict:
    assert len(api.calls) == 1
    results = api.calls[0]["results"]
    assert len(results) == 1
    return results[0]


def _results(api: FakeAPI) -> list[dict]:
    assert len(api.calls) == 1
    return api.calls[0]["results"]


# ---------------------------------------------------------------------- ID_RE


def test_id_re_matches_only_six_to_eight_digit_hashes():
    for text in ["#482913", "#12345678"]:
        assert compare_inline.ID_RE.fullmatch(text) is not None, text
    for text in ["#python", "#48291", "482913", "#123456789", "", "#48291a3"]:
        assert compare_inline.ID_RE.fullmatch(text) is None, text


# ------------------------------------------------------- multi-result payload


def _button_url(result: dict) -> str:
    """url единственной кнопки в ``reply_markup`` результата."""
    return result["reply_markup"]["inline_keyboard"][0][0]["url"]


def _assert_short_app_token(url: str, short: str = SHORT_NAME) -> None:
    """``https://t.me/<bot>/<short>?startapp=<id>_<sig>`` — отдельное мини-приложение."""
    prefix = f"https://t.me/{settings.bot_username}/{short}?startapp="
    assert url.startswith(prefix)
    token = url[len(prefix):]
    assert START_TOKEN_RE.fullmatch(token) is not None
    assert token.startswith(f"{CMP_ID}_")
    assert len(token) <= 64
    # Без экранированной/литеральной точки: Telegram ждёт base64url.
    assert "%2E" not in url and "." not in token


def _assert_main_app_token(url: str) -> None:
    """``https://t.me/<bot>?startapp=<id>_<sig>`` — Main Mini App (без short name)."""
    prefix = f"https://t.me/{settings.bot_username}?startapp="
    assert url.startswith(prefix)
    token = url[len(prefix):]
    assert START_TOKEN_RE.fullmatch(token) is not None
    assert token.startswith(f"{CMP_ID}_")
    assert "/" not in url[len("https://t.me/"):]


async def test_found_comparison_returns_photo_and_open_with_short_name(
    monkeypatch: pytest.MonkeyPatch,
):
    """Порядок: фото «Вставить превью» с кнопкой → article «Открыть сравнение»."""
    monkeypatch.setattr(settings, "cmp_webapp_short_name", SHORT_NAME)
    store = FakeStore(cached(preview_file_id=PREVIEW_FILE_ID))
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    call = api.calls[0]
    assert call["cache_time"] == 0
    assert call["is_personal"] is True

    results = _results(api)
    assert [result["type"] for result in results] == ["photo", "article"]
    assert [result["id"] for result in results] == [
        f"{CMP_ID}-photo",
        f"{CMP_ID}-open",
    ]

    first, second = settings.cmp_labels_pair
    label_line = f"Сравнение #{CMP_ID} · {first} / {second}"

    photo, open_result = results
    assert photo["title"] == "Вставить превью"
    assert photo["description"] == label_line
    assert photo["caption"] == label_line
    assert photo["photo_file_id"] == PREVIEW_FILE_ID
    _assert_short_app_token(_button_url(photo))

    assert open_result["title"] == "Открыть сравнение"
    assert open_result["description"] == "Полноразмерное сравнение в мини-приложении"
    assert open_result["input_message_content"] == {"message_text": label_line}
    assert open_result["reply_markup"] == photo["reply_markup"]
    button = open_result["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Открыть сравнение"
    _assert_short_app_token(button["url"])

    # Текстового результата в этой выдаче нет: выбрать его вместо фото нельзя.
    assert not any(
        result.get("title") == f"Сравнение #{CMP_ID} · только текст" for result in results
    )
    assert all(result["id"] != f"{CMP_ID}-text" for result in results)


async def test_main_app_link_when_short_name_is_empty():
    """Пустой short name — Main Mini App: ``t.me/<bot>?startapp=<token>``."""
    store = FakeStore(cached(preview_file_id=PREVIEW_FILE_ID))
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    results = _results(api)
    assert [result["id"] for result in results] == [f"{CMP_ID}-photo", f"{CMP_ID}-open"]
    photo, open_result = results
    _assert_main_app_token(_button_url(photo))
    assert open_result["reply_markup"] == photo["reply_markup"]


async def test_photo_result_absent_without_preview_file_id(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "cmp_webapp_short_name", SHORT_NAME)
    for preview_file_id in (None, ""):
        store = FakeStore(cached(preview_file_id=preview_file_id))
        api = FakeAPI()
        configure(store, api)

        assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

        results = _results(api)
        # Старая запись без превью: остаётся article с кнопкой, текстового нет.
        assert [result["id"] for result in results] == [f"{CMP_ID}-open"]
        assert all("photo_file_id" not in result for result in results)
        _assert_short_app_token(_button_url(results[0]))


async def test_only_text_when_bot_username_is_missing(monkeypatch: pytest.MonkeyPatch):
    """Нет ни превью, ни мини-аппа (пустой BOT_USERNAME) — честный текстовый фолбэк."""
    monkeypatch.setattr(settings, "bot_username", "")
    store = FakeStore(cached())
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    results = _results(api)
    assert [result["id"] for result in results] == [f"{CMP_ID}-text"]
    text_result = results[0]
    assert text_result["type"] == "article"
    assert text_result["title"] == f"Сравнение #{CMP_ID} · только текст"
    assert "reply_markup" not in text_result
    assert "html" not in text_result["input_message_content"]
    message_text = text_result["input_message_content"]["message_text"]
    assert message_text.startswith(f"Сравнение #{CMP_ID}")
    assert "Откройте бота в личке" in message_text


def test_start_token_is_short_base64url_and_stable():
    token = compare_inline._start_token(CMP_ID)

    # ``re.fullmatch(r'[0-9]+_[A-Za-z0-9_-]{16,}', token)`` — только base64url.
    assert START_TOKEN_RE.fullmatch(token) is not None
    assert re.fullmatch(r"[0-9]+_[A-Za-z0-9_-]{16,}", token) is not None
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token) is not None
    assert token.split("_", 1)[0] == CMP_ID
    assert len(token.split("_", 1)[1]) >= 16
    assert len(token) <= 64
    # Один и тот же номер — одна и та же подпись (Telegram кеширует мини-апп).
    assert compare_inline._start_token(CMP_ID) == token
    # Разные номера подписаны по-разному.
    other = compare_inline._start_token("999999")
    assert other.split("_", 1)[1] != token.split("_", 1)[1]


# ------------------------------------------------------------- found / missing


async def test_own_comparison_returns_open_article():
    """Без превью остаётся один article «Открыть сравнение» с кнопкой."""
    store = FakeStore(cached())
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    assert store.calls == [(CMP_ID, USER_ID)]
    call = api.calls[0]
    assert call["query_id"] == QUERY_ID
    assert call["cache_time"] == 0
    assert call["is_personal"] is True

    article = _article(api)
    assert article["type"] == "article"
    assert article["id"] == f"{CMP_ID}-open"
    assert article["title"] == "Открыть сравнение"
    # По умолчанию widget-контента в inline нет: бета его отклоняет.
    assert set(article["input_message_content"]) == {"message_text"}
    assert article["input_message_content"]["message_text"].startswith(
        f"Сравнение #{CMP_ID}"
    )
    _assert_main_app_token(_button_url(article))


async def test_widget_result_included_when_enabled(monkeypatch: pytest.MonkeyPatch):
    """CMP_INLINE_WIDGET=true — шторка возвращается последним результатом."""
    monkeypatch.setattr(settings, "cmp_inline_widget", True)
    store = FakeStore(cached())
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    results = _results(api)
    assert [result["id"] for result in results] == [f"{CMP_ID}-open", CMP_ID]
    widget = results[-1]
    assert "html" in widget["input_message_content"]
    assert widget["input_message_content"]["width"] == 0


class WidgetRefusingAPI(FakeAPI):
    """Bot API, который отклоняет widget-контент (как бета в inline)."""

    async def answer_inline(self, query_id, results, *, cache_time=0, is_personal=True):
        if any("html" in (result.get("input_message_content") or {}) for result in results):
            raise WidgetAPIError(400, "INPUT_MEDIA_WIDGET_NOT_ALLOWED")
        await super().answer_inline(
            query_id, results, cache_time=cache_time, is_personal=is_personal
        )


async def test_refused_widget_is_retried_without_it(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "cmp_inline_widget", True)
    monkeypatch.setattr(settings, "cmp_webapp_short_name", SHORT_NAME)
    store = FakeStore(cached(preview_file_id=PREVIEW_FILE_ID))
    api = WidgetRefusingAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    # Первый ответ со шторкой сервер отклонил; в итоге ушла выдача без неё.
    assert len(api.calls) == 1
    results = _results(api)
    assert [result["id"] for result in results] == [
        f"{CMP_ID}-photo",
        f"{CMP_ID}-open",
    ]
    assert all("html" not in (result.get("input_message_content") or {}) for result in results)


async def test_foreign_comparison_without_share_is_missing(monkeypatch: pytest.MonkeyPatch):
    """Чужое сравнение (даже с превью) не отдаёт ни file_id, ни кнопку."""
    monkeypatch.setattr(settings, "cmp_webapp_short_name", SHORT_NAME)
    store = FakeStore(cached(owner_id=OTHER_USER_ID, preview_file_id=PREVIEW_FILE_ID))
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    # owner_id передан: чужую запись кеш не отдаёт.
    assert store.calls == [(CMP_ID, USER_ID)]
    results = _results(api)
    assert [result["id"] for result in results] == ["cmp-missing"]
    assert all("photo_file_id" not in result for result in results)
    assert all("reply_markup" not in result for result in results)

    article = results[0]
    assert article["title"] == f"Сравнение #{CMP_ID} не найдено"
    assert "Номер не найден" in article["description"]
    assert api.calls[0]["is_personal"] is True


async def test_share_by_id_finds_foreign_comparison(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "cmp_share_by_id", True)
    store = FakeStore(cached(owner_id=OTHER_USER_ID))
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    assert store.calls == [(CMP_ID, None)]
    assert _article(api)["id"] == f"{CMP_ID}-open"


async def test_unknown_number_returns_missing_hint():
    store = FakeStore()
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query("#999999")) is True

    article = _article(api)
    assert article["id"] == "cmp-missing"
    assert article["title"] == "Сравнение #999999 не найдено"
    assert "#999999" in article["input_message_content"]["message_text"]
    assert api.calls[0]["is_personal"] is True


# --------------------------------------------------------------- short numbers


async def test_short_number_returns_input_hint():
    for text in ("#", "#12", "#12345"):
        store = FakeStore()
        api = FakeAPI()
        configure(store, api)

        assert await compare_inline.try_answer(make_query(text)) is True

        article = _article(api)
        assert article["title"] == "Введите номер сравнения", text
        message_text = article["input_message_content"]["message_text"]
        assert "Например: #482913" in message_text
        assert api.calls[0]["cache_time"] == 0
        assert api.calls[0]["is_personal"] is True
        assert store.calls == []


# ------------------------------------------------------------- ordinary queries


async def test_ordinary_query_is_not_ours():
    for text in ("объясни матрицу", "#python", "#482913abc", "482913", "  объясни  "):
        store = FakeStore()
        api = FakeAPI()
        configure(store, api)

        assert await compare_inline.try_answer(make_query(text)) is False, text
        assert api.calls == []
        assert store.calls == []


# ------------------------------------------------------------- error / no wiring


async def test_widget_api_errors_are_swallowed(monkeypatch: pytest.MonkeyPatch):
    """Протухший query ID не повод уходить в DeepSeek, падать или терять номер."""
    monkeypatch.setattr(settings, "cmp_webapp_short_name", SHORT_NAME)

    api = FakeAPI(error=WidgetAPIError(400, "QUERY_ID_INVALID"))
    configure(FakeStore(cached()), api)
    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    api = FakeAPI(error=WidgetAPIError(400, "QUERY_ID_INVALID"))
    configure(FakeStore(cached(preview_file_id=PREVIEW_FILE_ID)), api)
    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True
    assert [result["id"] for result in _results(api)] == [
        f"{CMP_ID}-photo",
        f"{CMP_ID}-open",
    ]

    api = FakeAPI(error=WidgetAPIError(400, "QUERY_ID_INVALID"))
    configure(FakeStore(), api)
    assert await compare_inline.try_answer(make_query("#999999")) is True
    assert _article(api)["id"] == "cmp-missing"


async def test_without_configure_returns_false():
    # Синглтоны очищены фикстурой: get_api() бросит RuntimeError.
    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is False


# ------------------------------------------------------------------- handler


def test_handler_tries_number_before_min_length_and_process():
    source = (ROOT / "bot" / "handlers" / "inline.py").read_text(encoding="utf-8")
    assert "from ..compare import inline as compare_inline" in source

    allowed = source.index("if not is_allowed(query.from_user.id):")
    branch = source.index("await compare_inline.try_answer(query)")
    min_length = source.index("len(text) < MIN_QUERY_LEN")
    process = source.index("asyncio.create_task(_process(query, text))")

    assert allowed < branch < min_length < process
