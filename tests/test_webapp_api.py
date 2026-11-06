"""Тесты HTTP API мини-аппа: данные сравнения и оригиналы по `Accept`.

Сеть не используется: приложение поднимается `aiohttp.test_utils.TestServer`
на loopback. Кеш и оригиналы — настоящие, во временном каталоге `tmp_path`:
оригиналы пишутся `bot.compare.originals.save`, поэтому тест проверяет и
чтение файлов с диска.

Валидный `initData` собирается здесь же независимой реализацией алгоритма
Bot API (как в `tests/test_compare_webauth.py`), чтобы проверка сервера
не проверяла сама себя.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, features

from bot.compare import originals, webauth
from bot.compare.store import ComparisonStore
from bot.config import settings
from bot.webapp.api import build_webapp_app

BOT_TOKEN = settings.telegram_bot_token
OWNER_ID = 7
FOREIGN_ID = 999
#: Номер, которого заведомо нет в кеше (8 цифр: короче не совпадёт со случайным).
MISSING_ID = "99999999"


# --------------------------------------------------------------------- helpers


def make_init_data(
    *,
    user_id: int = OWNER_ID,
    bot_token: str = BOT_TOKEN,
    auth_date: int | None = None,
) -> str:
    """Собирает `initData` с правильным `hash` (алгоритм Telegram WebApp)."""
    fields: dict[str, str] = {
        "user": json.dumps(
            {"id": user_id, "first_name": "Кир", "username": "kir"}, ensure_ascii=False
        ),
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "query-1",
    }
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


def picture(size: tuple[int, int], color: tuple[int, int, int]) -> Image.Image:
    """Гладкая синтетическая картинка нужного размера (Pillow кодирует её по-настоящему)."""
    small = Image.new("RGB", (6, 4))
    pixels = small.load()
    for y in range(4):
        for x in range(6):
            pixels[x, y] = tuple(
                min(255, channel + (x + y) * 8) for channel in color
            )
    return small.resize(size, Image.BICUBIC)


def url_for(cmp_id: str, side: str = "a") -> str:
    return f"/app/cmp/img/{webauth.sign(cmp_id)}/{side}"


def auth(init_data: str | None = None) -> dict[str, str]:
    return {"Authorization": f"tma {init_data or make_init_data()}"}


# ------------------------------------------------------------------- fixtures


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
async def store(tmp_path: Path):
    store = ComparisonStore(tmp_path / "comparisons.sqlite3")
    yield store
    await store.close()


@pytest.fixture
async def cmp_id(store: ComparisonStore, cache_dir: Path) -> str:
    """Реальное сравнение в кеше + оригиналы WebP/AVIF на диске."""
    img_a = picture((420, 280), (200, 40, 40))
    img_b = picture((360, 280), (40, 180, 60))
    cmp_id = await store.save(
        OWNER_ID,
        image_a=b"webp-a",
        image_b=b"webp-b",
        width=430,
        quality=70,
        labels=("До", "После"),
        zlib_bytes=1,
        aspect="3:2",
    )
    originals.save(cache_dir, cmp_id, img_a, img_b, width=300)
    return cmp_id


@pytest.fixture
def app(store: ComparisonStore, cache_dir: Path):
    return build_webapp_app(
        store=store,
        cache_dir=cache_dir,
        bot_token=BOT_TOKEN,
    )


@pytest.fixture
async def client(app):
    async with TestClient(TestServer(app)) as client:
        yield client


# -------------------------------------------------------------------- healthz


async def test_healthz_returns_ok(client: TestClient) -> None:
    response = await client.get("/healthz")
    assert response.status == 200
    assert await response.text() == "ok"


# ----------------------------------------------------------------------- data


async def test_data_with_authorization_header(client: TestClient, cmp_id: str) -> None:
    """Стандартный путь Telegram: `Authorization: tma <initData>`."""
    token = webauth.sign(cmp_id)
    response = await client.get(f"/api/cmp/{token}", headers=auth())
    assert response.status == 200

    payload = await response.json()
    assert set(payload) == {"id", "labels", "aspect", "width", "height", "images"}
    assert payload["id"] == cmp_id
    assert payload["labels"] == ["До", "После"]
    assert payload["aspect"] == "3:2"
    assert payload["width"] == 430
    assert payload["height"] == 287  # round(430 * 2 / 3)
    assert payload["images"] == {
        "a": f"/app/cmp/img/{token}/a",
        "b": f"/app/cmp/img/{token}/b",
    }
    assert response.headers["Cache-Control"] == "no-store"


async def test_data_with_query_init_data(client: TestClient, cmp_id: str) -> None:
    """Запасной путь: `?init_data=<initData>` в query."""
    response = await client.get(
        f"/api/cmp/{webauth.sign(cmp_id)}",
        params={"init_data": make_init_data()},
    )
    assert response.status == 200
    assert (await response.json())["id"] == cmp_id


async def test_data_without_init_data_is_unauthorized(
    client: TestClient, cmp_id: str
) -> None:
    response = await client.get(f"/api/cmp/{webauth.sign(cmp_id)}")
    assert response.status == 401
    assert "error" in await response.json()


async def test_data_with_broken_init_data_is_unauthorized(
    client: TestClient, cmp_id: str
) -> None:
    response = await client.get(
        f"/api/cmp/{webauth.sign(cmp_id)}",
        headers={"Authorization": "tma user=%7B%7D&auth_date=1&hash=deadbeef"},
    )
    assert response.status == 401
    assert "error" in await response.json()


async def test_data_for_foreign_user_is_forbidden(
    client: TestClient, cmp_id: str
) -> None:
    response = await client.get(
        f"/api/cmp/{webauth.sign(cmp_id)}",
        headers=auth(make_init_data(user_id=FOREIGN_ID)),
    )
    assert response.status == 403
    assert "error" in await response.json()


async def test_data_share_by_id_allows_foreign_user(
    store: ComparisonStore, cache_dir: Path, cmp_id: str
) -> None:
    """При `share_by_id=True` номер сравнения работает как ссылка-доступ."""
    app = build_webapp_app(
        store=store,
        cache_dir=cache_dir,
        bot_token=BOT_TOKEN,
        share_by_id=True,
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            f"/api/cmp/{webauth.sign(cmp_id)}",
            headers=auth(make_init_data(user_id=FOREIGN_ID)),
        )
    assert response.status == 200


async def test_data_with_broken_token_is_forbidden(
    client: TestClient, cmp_id: str
) -> None:
    response = await client.get(
        f"/api/cmp/{cmp_id}_AAAAAAAAAAAAAAAAAAAA", headers=auth()
    )
    assert response.status == 403
    assert "error" in await response.json()


async def test_data_for_unknown_comparison_is_not_found(
    client: TestClient,
) -> None:
    response = await client.get(
        f"/api/cmp/{webauth.sign(MISSING_ID)}", headers=auth()
    )
    assert response.status == 404
    assert "error" in await response.json()


# --------------------------------------------------------------------- images


async def test_image_avif_when_accepted(client: TestClient, cmp_id: str) -> None:
    if not features.check("avif"):
        pytest.skip("Pillow без AVIF — тест про AVIF не применим")

    response = await client.get(url_for(cmp_id), headers={"Accept": "image/avif"})
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("image/avif")
    body = await response.read()
    with Image.open(BytesIO(body)) as decoded:
        assert decoded.format == "AVIF"


async def test_image_webp_without_accept(client: TestClient, cmp_id: str) -> None:
    """Без `Accept: image/avif` отдаётся WebP (фолбэк для старых WebView)."""
    response = await client.get(url_for(cmp_id))
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("image/webp")
    assert response.headers["Vary"] == "Accept"
    assert "immutable" in response.headers["Cache-Control"]
    assert response.headers["ETag"].startswith('"')
    body = await response.read()
    assert body[:4] == b"RIFF" and body[8:12] == b"WEBP"


async def test_image_webp_when_explicitly_accepted(
    client: TestClient, cmp_id: str
) -> None:
    response = await client.get(url_for(cmp_id), headers={"Accept": "image/webp"})
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("image/webp")


async def test_image_avif_falls_back_to_webp_without_file(
    client: TestClient, cache_dir: Path, cmp_id: str
) -> None:
    """AVIF-файла нет — при `Accept: image/avif` всё равно отдаётся WebP."""
    for side in originals.SIDES:
        (cache_dir / "originals" / f"{cmp_id}-{side}.avif").unlink(missing_ok=True)

    response = await client.get(url_for(cmp_id), headers={"Accept": "image/avif"})
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("image/webp")


async def test_image_not_modified_on_repeated_etag(
    client: TestClient, cmp_id: str
) -> None:
    first = await client.get(url_for(cmp_id), headers={"Accept": "image/webp"})
    assert first.status == 200
    etag = first.headers["ETag"]

    second = await client.get(
        url_for(cmp_id),
        headers={"Accept": "image/webp", "If-None-Match": etag},
    )
    assert second.status == 304
    assert await second.read() == b""
    assert second.headers["ETag"] == etag
    assert second.headers["Vary"] == "Accept"


async def test_image_with_unknown_side_is_not_found(
    client: TestClient, cmp_id: str
) -> None:
    response = await client.get(url_for(cmp_id, "c"))
    assert response.status == 404


async def test_image_with_broken_token_is_forbidden(client: TestClient) -> None:
    response = await client.get(f"/app/cmp/img/{MISSING_ID}_AAAAAAAAAAAAAAAAAAAA/a")
    assert response.status == 403


async def test_image_for_unknown_comparison_is_not_found(
    client: TestClient,
) -> None:
    response = await client.get(url_for(MISSING_ID))
    assert response.status == 404


@pytest.mark.parametrize(
    "path",
    [
        "/app/cmp/img/../../etc/passwd",
        f"/app/cmp/img/{webauth.sign(MISSING_ID)}/..%2F..%2Fetc%2Fpasswd",
        f"/app/cmp/img/{webauth.sign(MISSING_ID)}/..%5C..%5Cetc%5Cpasswd",
    ],
)
async def test_image_path_traversal_is_not_served(
    client: TestClient, path: str
) -> None:
    """Только `a`/`b`: наружу из каталога оригиналов выйти нельзя."""
    response = await client.get(path)
    assert response.status in (403, 404)


# ---------------------------------------------------------------------- page


async def test_page_without_static_dir_is_not_found(client: TestClient) -> None:
    for path in ("/app/cmp", "/app/cmp/"):
        response = await client.get(path)
        assert response.status == 404


async def test_page_served_from_static_dir(
    store: ComparisonStore, cache_dir: Path, tmp_path: Path
) -> None:
    """Роут готов для шага 6: файл есть — отдаётся HTML, нет — 404."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "cmp.html").write_text("<!doctype html><title>cmp</title>", encoding="utf-8")
    app = build_webapp_app(
        store=store,
        cache_dir=cache_dir,
        bot_token=BOT_TOKEN,
        static_dir=static_dir,
    )
    async with TestClient(TestServer(app)) as client:
        for path in ("/app/cmp", "/app/cmp/"):
            response = await client.get(path)
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/html")
            assert "cmp" in await response.text()
