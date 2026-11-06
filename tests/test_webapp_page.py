"""Тесты страницы мини-аппа `/app/cmp`: один самодостаточный HTML из пакета.

Страница не собирается и не тянет внешних файлов, поэтому проверяется и сам
файл (обязательные подстроки, отсутствие внешних ресурсов, отсутствие ссылок
на `cmp.js`/`cmp.css`), и его отдача роутом `/app/cmp` через настоящий
`build_webapp_app` на `TestServer`. Сеть не используется: сервер слушает
loopback, кеш — во временном каталоге `tmp_path`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.compare.store import ComparisonStore
from bot.config import settings
from bot.webapp import api as webapp_api

#: Каталог статики — внутри пакета, а не в корне репозитория.
STATIC_DIR = Path(webapp_api.__file__).resolve().parent / "static"
PAGE_FILE = STATIC_DIR / "cmp.html"
PAGE = PAGE_FILE.read_text(encoding="utf-8")
#: Единственный разрешённый внешний ресурс: официальный скрипт Telegram WebApp.
TELEGRAM_SCRIPT = "https://telegram.org/js/telegram-web-app.js"


@pytest.fixture
async def client(tmp_path: Path):
    store = ComparisonStore(tmp_path / "comparisons.sqlite3")
    app = webapp_api.build_webapp_app(
        store=store,
        cache_dir=tmp_path / "cache",
        bot_token=settings.telegram_bot_token,
        static_dir=STATIC_DIR,
    )
    async with TestClient(TestServer(app)) as client:
        yield client
    await store.close()


# ------------------------------------------------------------------- статика


def test_page_is_packaged_with_the_webapp() -> None:
    """`cmp.html` лежит в пакете `bot/webapp/static` и читается как UTF-8."""
    assert STATIC_DIR.parent.name == "webapp"
    assert PAGE_FILE.is_file()
    assert PAGE_FILE.stat().st_size > 8000
    assert PAGE.lstrip().lower().startswith("<!doctype html>")


def test_page_has_telegram_webapp_markup() -> None:
    """Метаданные и оболочка: мобильный viewport, канвас, чипы, кнопка сброса."""
    assert 'name="viewport"' in PAGE
    assert "width=device-width" in PAGE
    assert "initial-scale=1" in PAGE
    assert "viewport-fit=cover" in PAGE
    assert "<canvas" in PAGE
    assert 'id="chipA"' in PAGE and ">До<" in PAGE
    assert 'id="chipB"' in PAGE and ">После<" in PAGE
    assert 'id="reset"' in PAGE and "Сброс" in PAGE


@pytest.mark.parametrize(
    "needle",
    [
        # Telegram API: без него нет ни initData, ни темы.
        'src="' + TELEGRAM_SCRIPT + '"',
        "window.Telegram",
        "Telegram.WebApp",
        "initDataUnsafe",
        "start_param",
        "initData",
        "themeParams",
        "tg.ready()",
        "tg.expand()",
        # Данные сравнения и авторизация по initData.
        "/api/cmp/",
        "Authorization",
        "'tma '",
        "?t=",
        # Шторка и зум.
        "getContext('2d')",
        "ctx.clip()",
        "drawImage",
        "setPointerCapture",
        "touch-action:none",
        "dblclick",
        "wheel",
        "devicePixelRatio",
        "screen.width",
        "zoom",
        "resetView",
    ],
)
def test_page_implements_required_behaviour(needle: str) -> None:
    assert needle in PAGE


@pytest.mark.parametrize(
    "needle",
    [
        "Откройте сравнение из чата",
        "Ссылка недействительна",
        "Нет связи",
        "Повторить",
        "Загружаем сравнение",
    ],
)
def test_page_has_error_and_loading_screens(needle: str) -> None:
    assert needle in PAGE


def test_page_uses_no_external_resources_except_telegram() -> None:
    """Внешний `telegram-web-app.js` — единственный внешний ресурс страницы."""
    scripts = re.findall(
        r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', PAGE, re.IGNORECASE
    )
    assert scripts == [TELEGRAM_SCRIPT]

    assert re.search(r"<link\b", PAGE, re.IGNORECASE) is None
    assert re.search(r"<style\b[^>]*\bsrc\s*=", PAGE, re.IGNORECASE) is None
    assert "@import" not in PAGE

    urls = re.findall(
        r'(?:\bsrc|\bhref)\s*=\s*["\'](https?://[^"\']+)["\']', PAGE, re.IGNORECASE
    )
    assert set(urls) == {TELEGRAM_SCRIPT}
    assert re.findall(r"url\(\s*[\"']?(https?://[^)\"']+)", PAGE, re.IGNORECASE) == []


def test_page_needs_no_separate_assets() -> None:
    """Страница самодостаточна: ни `cmp.js`, ни `cmp.css`, ни сборки."""
    assert "cmp.js" not in PAGE
    assert "cmp.css" not in PAGE
    assert not (STATIC_DIR / "cmp.js").exists()
    assert not (STATIC_DIR / "cmp.css").exists()
    assert sorted(path.name for path in STATIC_DIR.iterdir()) == ["cmp.html"]


# ------------------------------------------------------------------- роутинг


async def test_page_is_served_as_html(client: TestClient) -> None:
    """`GET /app/cmp` и `/app/cmp/` отдают страницу из пакета как `text/html`."""
    for path in ("/app/cmp", "/app/cmp/"):
        response = await client.get(path)
        assert response.status == 200
        content_type = response.headers["Content-Type"]
        assert content_type.startswith("text/html")
        assert response.headers.get("Cache-Control") == "no-cache"

        body = await response.text()
        assert body == PAGE
        assert '<meta charset="utf-8">' in body
        for needle in (
            TELEGRAM_SCRIPT,
            "Telegram.WebApp",
            "/api/cmp/",
            "start_param",
            "<canvas",
            "themeParams",
        ):
            assert needle in body


async def test_page_keeps_api_404_without_static_dir(
    tmp_path: Path,
) -> None:
    """Без `static_dir` страницы нет: поведение API-тестов не меняется."""
    store = ComparisonStore(tmp_path / "comparisons.sqlite3")
    app = webapp_api.build_webapp_app(
        store=store,
        cache_dir=tmp_path / "cache",
        bot_token=settings.telegram_bot_token,
    )
    async with TestClient(TestServer(app)) as client:
        for path in ("/app/cmp", "/app/cmp/"):
            response = await client.get(path)
            assert response.status == 404
    await store.close()
