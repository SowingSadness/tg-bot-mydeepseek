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
import logging
import re
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web_runner import AppRunner
from PIL import Image, features

from bot.compare import originals, webauth
from bot.compare.store import ComparisonStore
from bot.config import settings
from bot.webapp.api import CLIENT_LOG_PATH, CLIENT_LOGS_KEY, build_webapp_app, log_requests

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


class QuietTestServer(TestServer):
    """`TestServer` без access-лога — как `AppRunner(app, access_log=None)` в `bot/main.py`.

    Иначе aiohttp пишет в `caplog` полный URL с `startapp`-токеном, и проверка
    «в логах нет токена» проверяла бы не наш код, а стандартный лог.
    """

    async def _make_runner(self, **kwargs):
        kwargs["access_log"] = None
        return AppRunner(self.app, **kwargs)


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
    async with TestClient(QuietTestServer(app)) as client:
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


# ------------------------------------------------------- журнал запросов


#: Строка запроса middleware: `<status> <METHOD> <url> — <N> мс[ cmp=…][ user=…]`.
REQUEST_LINE_RE = re.compile(
    r"^\d{3} [A-Z]+ \S+ — \d+ мс(?: cmp=\S+)?(?: user=-?\d+)?$"
)


def webapp_lines(
    caplog: pytest.LogCaptureFixture, *, method: str | None = None
) -> list[logging.LogRecord]:
    """Записи middleware — только строки запроса, без `webapp[client]`-payload."""
    lines = [
        record
        for record in caplog.records
        if REQUEST_LINE_RE.match(record.getMessage())
    ]
    if method is not None:
        lines = [record for record in lines if f" {method} " in record.getMessage()]
    return lines


async def test_request_logged_at_info_with_visible_token(
    client: TestClient, cmp_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Один `INFO`-рядок о запросе: `<status> <METHOD> <url> — <N> мс` + `user=`.

    Токен `startapp` в URL виден по требованию заказчика, `init_data` — нет.
    """
    token = webauth.sign(cmp_id)
    with caplog.at_level(logging.INFO):
        response = await client.get(f"/api/cmp/{token}", headers=auth())
    assert response.status == 200

    lines = webapp_lines(caplog, method="GET")
    assert len(lines) == 1
    line = lines[0].getMessage()
    assert lines[0].levelno == logging.INFO
    assert line.startswith(f"200 GET /api/cmp/{token} — ")
    assert f"user={OWNER_ID}" in line
    assert "init_data" not in caplog.text
    assert BOT_TOKEN not in caplog.text


async def test_query_init_data_value_is_redacted_in_log(
    client: TestClient, cmp_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """`?init_data=` в лог не попадает (значение маскируется)."""
    init_data = make_init_data()
    with caplog.at_level(logging.INFO):
        response = await client.get(
            f"/api/cmp/{webauth.sign(cmp_id)}", params={"init_data": init_data}
        )
    assert response.status == 200
    assert init_data not in caplog.text
    assert "init_data=<скрыто>" in caplog.text
    lines = webapp_lines(caplog, method="GET")
    assert len(lines) == 1
    assert lines[0].getMessage().startswith("200 GET /api/cmp/")
    assert f"user={OWNER_ID}" in lines[0].getMessage()
    assert BOT_TOKEN not in caplog.text


async def test_broken_token_logged_as_warning(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    token = f"{MISSING_ID}_AAAAAAAAAAAAAAAAAAAA"
    with caplog.at_level(logging.INFO):
        response = await client.get(f"/api/cmp/{token}", headers=auth())
    assert response.status == 403

    warnings = [
        record for record in webapp_lines(caplog) if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    line = warnings[0].getMessage()
    assert line.startswith(f"403 GET /api/cmp/{token} — ")
    assert "user=" not in line  # 403 не даёт проверенного пользователя


async def test_http_exception_logged_as_single_warning(
    store: ComparisonStore, cache_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`web.HTTPException` из хендлера: одна WARNING-строка с его статусом."""
    app = build_webapp_app(store=store, cache_dir=cache_dir, bot_token=BOT_TOKEN)

    async def forbidden(request: web.Request) -> web.Response:
        raise web.HTTPForbidden()

    app.router.add_get("/forbidden", forbidden)
    async with TestClient(QuietTestServer(app)) as client:
        with caplog.at_level(logging.INFO):
            response = await client.get("/forbidden")
    assert response.status == 403

    warnings = [
        record for record in webapp_lines(caplog) if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith("403 GET /forbidden — ")


async def test_unhandled_exception_logged_as_error(
    store: ComparisonStore, cache_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Необработанное исключение хендлера: одна ERROR-строка со статусом 500."""
    app = build_webapp_app(store=store, cache_dir=cache_dir, bot_token=BOT_TOKEN)

    async def boom(request: web.Request) -> web.Response:
        raise RuntimeError("boom")

    app.router.add_get("/boom", boom)
    async with TestClient(QuietTestServer(app)) as client:
        with caplog.at_level(logging.INFO):
            response = await client.get("/boom")
    assert response.status == 500

    errors = [
        record for record in webapp_lines(caplog) if record.levelno == logging.ERROR
    ]
    assert len(errors) == 1
    assert errors[0].getMessage().startswith("500 GET /boom — ")



async def test_healthz_logged_at_info(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        response = await client.get("/healthz")
    assert response.status == 200

    lines = [
        record
        for record in webapp_lines(caplog, method="GET")
        if "/healthz" in record.getMessage()
    ]
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    assert lines[0].getMessage().startswith("200 GET /healthz — ")
    assert "user=" not in lines[0].getMessage()


# --------------------------------------------------- клиентские отчёты


async def test_client_log_fields_are_redacted_and_single_line(
    client: TestClient, cmp_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """`message`/`where` схлопываются в одну строку и теряют токен."""
    token = webauth.sign(cmp_id)
    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/cmp/log",
            json={
                "token": token,
                "message": "boom\nINJECTED",
                "where": "482913_AbC-123_x\r\nFAKE",
            },
        )
    assert response.status == 204

    records = [
        record for record in caplog.records if "webapp[client]" in record.getMessage()
    ]
    assert len(records) == 1
    line = records[0].getMessage()
    assert "\n" not in line and "\r" not in line
    assert "boom INJECTED" in line  # текст остался, но в той же строке
    assert "\nINJECTED" not in caplog.text
    assert "482913_AbC-123_x" not in caplog.text
    assert "<token>" in line
    assert "FAKE" in line


async def test_client_log_written_as_error(
    client: TestClient, cmp_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Валидный токен: `204` и `ERROR`-строка `webapp[client]` с текстом."""
    token = webauth.sign(cmp_id)
    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/cmp/log",
            json={"token": token, "message": "boom", "where": "load"},
        )
    assert response.status == 204

    records = [
        record
        for record in caplog.records
        if "webapp[client]" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    line = records[0].getMessage()
    assert "boom" in line
    assert "where=load" in line
    assert f"cmp={cmp_id}" in line
    assert token not in caplog.text


async def test_client_log_level_warning(
    client: TestClient, cmp_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    token = webauth.sign(cmp_id)
    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/cmp/log",
            json={"token": token, "level": "warning", "message": "hm"},
        )
    assert response.status == 204
    records = [
        record
        for record in caplog.records
        if "webapp[client]" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


async def test_client_log_without_token_is_forbidden_and_silent(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        no_token = await client.post("/api/cmp/log", json={"message": "boom"})
        broken = await client.post(
            "/api/cmp/log", json={"token": "bad", "message": "boom"}
        )
    assert no_token.status == 403
    assert broken.status == 403
    assert "webapp[client]" not in caplog.text


async def test_client_log_message_too_long(
    client: TestClient, cmp_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    token = webauth.sign(cmp_id)
    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/cmp/log", json={"token": token, "message": "x" * 501}
        )
    assert response.status == 413
    assert "webapp[client]" not in caplog.text


async def test_client_log_body_too_large(
    client: TestClient, cmp_id: str
) -> None:
    """Тело больше `client_max_size` (8 КБ) — 413 от aiohttp."""
    token = webauth.sign(cmp_id)
    response = await client.post(
        "/api/cmp/log",
        json={"token": token, "message": "boom", "url": "u" * 9000},
    )
    assert response.status == 413


async def test_client_log_empty_message_is_bad_request(
    client: TestClient, cmp_id: str
) -> None:
    token = webauth.sign(cmp_id)
    for payload in (
        {"token": token, "message": "   "},
        {"token": token, "message": 42},
        {"token": token},
    ):
        response = await client.post("/api/cmp/log", json=payload)
        assert response.status == 400


async def test_client_log_rate_limited_to_30_per_minute(
    client: TestClient, cmp_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """31-е сообщение в минуту — `204`, но отчёт отброшен с одной `WARNING`-строкой."""
    token = webauth.sign(cmp_id)
    with caplog.at_level(logging.INFO):
        for index in range(31):
            response = await client.post(
                "/api/cmp/log",
                json={"token": token, "message": f"boom {index}", "where": "load"},
            )
            assert response.status == 204

    payload = [
        record
        for record in caplog.records
        if "webapp[client]" in record.getMessage()
        and "отброшен" not in record.getMessage()
    ]
    assert len(payload) == 30

    dropped = [
        record
        for record in caplog.records
        if "webapp[client]" in record.getMessage()
        and "отброшен" in record.getMessage()
    ]
    assert len(dropped) == 1
    assert dropped[0].levelno == logging.WARNING
    line = dropped[0].getMessage()
    assert f"cmp={cmp_id}" in line
    assert "лимит 30" in line
    assert "where=load" in line
    # Тело отброшенного отчёта в лог не попадает.
    assert "boom 30" not in caplog.text


async def test_client_log_request_line_is_error(
    client: TestClient, cmp_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Строка запроса `POST /api/cmp/log` — `ERROR` даже при `204`, с `cmp=`."""
    token = webauth.sign(cmp_id)
    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/cmp/log",
            json={"token": token, "message": "boom", "where": "render"},
        )
    assert response.status == 204

    lines = webapp_lines(caplog, method="POST")
    assert len(lines) == 1
    line = lines[0].getMessage()
    assert lines[0].levelno == logging.ERROR  # не INFO, хотя статус 204
    assert line.startswith("204 POST /api/cmp/log — ")
    assert f"cmp={cmp_id}" in line
    assert "init_data" not in caplog.text
    assert token not in caplog.text
    assert BOT_TOKEN not in caplog.text

    payload = [
        record for record in caplog.records if "webapp[client]" in record.getMessage()
    ]
    assert len(payload) == 1
    assert payload[0].levelno == logging.ERROR
    assert "where=render" in payload[0].getMessage()


class _FakeRequest(dict):
    """Двойник `web.Request` для прямого вызова middleware (умеет `request["key"]`)."""

    method = "POST"
    path = CLIENT_LOG_PATH
    path_qs = CLIENT_LOG_PATH


async def test_client_log_line_carries_cmp_and_user(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Middleware дописывает `cmp=`/`user=`, когда контекст запроса их знает."""
    request = _FakeRequest()
    request["webapp_cmp_id"] = 337326
    request["webapp_user_id"] = 128640797

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=204)

    with caplog.at_level(logging.INFO):
        response = await log_requests(request, handler)
    assert response.status == 204

    lines = webapp_lines(caplog, method="POST")
    assert len(lines) == 1
    assert lines[0].levelno == logging.ERROR
    assert lines[0].getMessage().startswith("204 POST /api/cmp/log — ")
    assert lines[0].getMessage().endswith(" cmp=337326 user=128640797")


async def test_exactly_one_request_line_per_request(
    client: TestClient, cmp_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Одна строка на запрос: `GET` — `INFO`, отчёт клиента — `ERROR`."""
    token = webauth.sign(cmp_id)
    with caplog.at_level(logging.INFO):
        assert (await client.get("/healthz")).status == 200
        assert (await client.get(f"/api/cmp/{token}", headers=auth())).status == 200
        assert (
            await client.post(
                "/api/cmp/log", json={"token": token, "message": "boom"}
            )
        ).status == 204

    lines = webapp_lines(caplog)
    assert len(lines) == 3
    assert [record.levelno for record in lines] == [
        logging.INFO,
        logging.INFO,
        logging.ERROR,
    ]


async def test_client_log_prunes_dead_keys_after_window(
    client: TestClient,
    app,
    cmp_id: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ключи лимита без свежих отчётов удаляются, лимит токена начинается заново."""
    token = webauth.sign(cmp_id)
    other = webauth.sign("12345")
    clock = [1000.0]
    monkeypatch.setattr("bot.webapp.api._now", lambda: clock[0])
    logs = app[CLIENT_LOGS_KEY]

    with caplog.at_level(logging.INFO):
        for index in range(30):
            response = await client.post(
                "/api/cmp/log", json={"token": token, "message": f"boom {index}"}
            )
            assert response.status == 204
    key = hashlib.sha256(token.encode()).hexdigest()
    other_key = hashlib.sha256(other.encode()).hexdigest()
    assert len(logs[key]) == 30

    clock[0] += 61.0
    with caplog.at_level(logging.INFO):
        probe = await client.post(
            "/api/cmp/log", json={"token": other, "message": "probe"}
        )
    assert probe.status == 204
    assert key not in logs
    assert other_key in logs

    with caplog.at_level(logging.INFO):
        for index in range(30):
            response = await client.post(
                "/api/cmp/log", json={"token": token, "message": f"again {index}"}
            )
            assert response.status == 204
    records = [
        record for record in caplog.records if "webapp[client]" in record.getMessage()
    ]
    assert len(records) == 30 + 1 + 30
