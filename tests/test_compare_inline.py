"""Тесты inline-ветки ``#номер``: без сети, кеш и Bot API — лёгкие фейки."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.compare import api as api_module
from bot.compare import build
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

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- helpers


def cached(cmp_id: str = CMP_ID, owner_id: int = USER_ID) -> Cached:
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
    """Синглтоны и режим доступа не протекают между тестами."""
    monkeypatch.setattr(service, "_store", None)
    monkeypatch.setattr(api_module, "_api", None)
    monkeypatch.setattr(settings, "cmp_share_by_id", False)
    yield


def _article(api: FakeAPI) -> dict:
    assert len(api.calls) == 1
    results = api.calls[0]["results"]
    assert len(results) == 1
    return results[0]


# ---------------------------------------------------------------------- ID_RE


@pytest.mark.parametrize("text", ["#482913", "#12345678"])
def test_id_re_accepts_numbers(text: str):
    assert compare_inline.ID_RE.fullmatch(text) is not None


@pytest.mark.parametrize(
    "text", ["#python", "#48291", "482913", "#123456789", "", "#48291a3"]
)
def test_id_re_rejects_everything_else(text: str):
    assert compare_inline.ID_RE.fullmatch(text) is None


# ------------------------------------------------------------- found / missing


async def test_own_comparison_returns_one_widget_article():
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
    assert article["id"] == CMP_ID
    assert article["title"] == f"Сравнение #{CMP_ID}"

    expected_html = build.render_cached(
        image_a=IMAGE_A,
        image_b=IMAGE_B,
        labels=settings.cmp_labels_pair,
        title=settings.cmp_title,
        hint=settings.cmp_hint,
    )
    assert article["input_message_content"] == {
        "html": expected_html,
        "width": 0,
        "height": 0,
    }


async def test_foreign_comparison_without_share_is_missing():
    store = FakeStore(cached(owner_id=OTHER_USER_ID))
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True

    # owner_id передан: чужую запись кеш не отдаёт.
    assert store.calls == [(CMP_ID, USER_ID)]
    article = _article(api)
    assert article["id"] == "cmp-missing"
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
    assert _article(api)["id"] == CMP_ID


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


@pytest.mark.parametrize("text", ["#", "#12", "#12345"])
async def test_short_number_returns_input_hint(text: str):
    store = FakeStore()
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(text)) is True

    article = _article(api)
    assert article["title"] == "Введите номер сравнения"
    message_text = article["input_message_content"]["message_text"]
    assert "Например: #482913" in message_text
    assert api.calls[0]["cache_time"] == 0
    assert api.calls[0]["is_personal"] is True
    assert store.calls == []


# ------------------------------------------------------------- ordinary queries


@pytest.mark.parametrize(
    "text", ["объясни матрицу", "#python", "#482913abc", "482913", "  объясни  "]
)
async def test_ordinary_query_is_not_ours(text: str):
    store = FakeStore()
    api = FakeAPI()
    configure(store, api)

    assert await compare_inline.try_answer(make_query(text)) is False
    assert api.calls == []
    assert store.calls == []


# ------------------------------------------------------------- error / no wiring


async def test_widget_api_error_is_swallowed():
    api = FakeAPI(error=WidgetAPIError(400, "QUERY_ID_INVALID"))
    configure(FakeStore(cached()), api)

    assert await compare_inline.try_answer(make_query(f"#{CMP_ID}")) is True


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
