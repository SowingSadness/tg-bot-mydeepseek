"""Тесты страницы мини-аппа `/app/cmp`: один самодостаточный HTML из пакета.

Страница не собирается и не тянет внешних файлов, поэтому проверяется и сам
файл (обязательные подстроки, отсутствие внешних ресурсов, отсутствие ссылок
на `cmp.js`/`cmp.css`), и его отдача роутом `/app/cmp` через настоящий
`build_webapp_app` на `TestServer`. Сеть не используется: сервер слушает
loopback, кеш — во временном каталоге `tmp_path`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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
#: Размер страницы до добавления репортёра ошибок (замер перед шагом 3), байт.
PAGE_BYTES_BEFORE_REPORTER = 25197
#: Репортёр не должен раздувать единственный файл страницы сильнее, чем на 3 КБ,
#: плюс 1 КБ на защиту отрисовки (первый кадр, `try/catch`, отчёт `where=render`),
#: плюс 2 КБ на чтение токена из query (`tgWebAppStartParam`/`startapp`) и
#: сигнал «нет токена» без токена.
PAGE_BYTES_BUDGET = PAGE_BYTES_BEFORE_REPORTER + 3 * 1024 + 1024 + 2 * 1024


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


def page_block(start: str, end: str) -> str:
    """Кусок страницы от `start` до ближайшего `end` после него."""
    begin = PAGE.index(start)
    return PAGE[begin : PAGE.index(end, begin)]


#: Node нужен только поведенческим тестам JS страницы; это не зависимость проекта
#: (страница остаётся одним HTML), поэтому без node они пропускаются, а статические
#: проверки источников/стража работают всегда.
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node недоступен")


def run_page_js(script: str) -> str:
    """Выполняет JS страницы в node и возвращает stdout (сеть не нужна)."""
    completed = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def run_start_token(
    search: str = "",
    tg_js: str = "{}",
    *,
    window_js: str | None = None,
    prelude: str = "",
) -> str:
    """`startToken()` страницы в node: подставляем `tg` и `window.location.search`."""
    if window_js is None:
        window_js = "{location: {search: " + json.dumps(search) + "}}"
    js = page_block("function queryParam(name)", "function telegramInitData()")
    return run_page_js(
        prelude
        + "var tg = " + tg_js + ";\n"
        + "var window = " + window_js + ";\n"
        + js
        + "\nprocess.stdout.write(startToken());"
    )


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


def test_page_has_required_strings() -> None:
    """Заявленные строки страницы: Telegram API, данные, шторка, экраны и отчёты."""
    needles = [
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
        # Экраны статуса.
        "Откройте сравнение из чата",
        "Ссылка недействительна",
        "Нет связи",
        "Повторить",
        "Загружаем сравнение",
        # Явные отчёты из мест, где ошибка уже поймана в `catch`/`onerror`.
        "report('error'",
        "report('warning'",
        "'window'",
        "'promise'",
        "'load'",
        "'image'",
        "'sdk'",
        "'Telegram.WebApp нет'",
    ]
    missing = [needle for needle in needles if needle not in PAGE]
    assert not missing, f"в cmp.html нет: {missing}"


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
    # Репортёр ошибок не тянет внешних библиотек: любой URL — только Telegram.
    assert set(re.findall(r"https?://[^\s\"'<>]+", PAGE)) == {TELEGRAM_SCRIPT}


def test_page_needs_no_separate_assets() -> None:
    """Страница самодостаточна: ни `cmp.js`, ни `cmp.css`, ни сборки."""
    assert "cmp.js" not in PAGE
    assert "cmp.css" not in PAGE
    assert not (STATIC_DIR / "cmp.js").exists()
    assert not (STATIC_DIR / "cmp.css").exists()
    assert sorted(path.name for path in STATIC_DIR.iterdir()) == ["cmp.html"]


# --------------------------------------------------- отчёт об ошибках клиента


def test_page_reports_client_errors_to_webapp_log() -> None:
    """Ошибки страницы уходят на `POST /api/cmp/log`, а не только в `console`.

    В WebView Telegram консоли не видно, поэтому репортёр шлёт JSON с токеном,
    уровнем, сообщением и местом ошибки, не дожидаясь закрытия страницы
    (`keepalive`), и глотает собственные сбои (`.catch`).
    """
    assert "/api/cmp/log" in PAGE
    assert "keepalive: true" in PAGE
    assert "'Content-Type': 'application/json'" in PAGE
    assert "unhandledrejection" in PAGE
    assert "addEventListener('error'" in PAGE
    assert "addEventListener('unhandledrejection'" in PAGE
    # Дедупликация: одна и та же ошибка не должна лететь дважды.
    assert "new Set()" in PAGE
    assert "reported.has(" in PAGE and "reported.add(" in PAGE
    # Отправка не должна порождать необработанный промис.
    assert ".catch(function () {" in PAGE
    # Без токена отправляется только сигнал «нет токена» (см. тест ниже).
    assert "if (!token && place !== 'token') return;" in PAGE


def test_page_size_stays_within_reporter_budget() -> None:
    """Репортёр, защита отрисовки и токен из query компактны: рост не больше 6 КБ."""
    size = PAGE_FILE.stat().st_size
    assert size <= PAGE_BYTES_BUDGET, (
        f"cmp.html вырос до {size} байт; бюджет — "
        f"{PAGE_BYTES_BUDGET} байт (до шага было {PAGE_BYTES_BEFORE_REPORTER})"
    )


# ------------------------------------------------------- токен и его источники


def test_start_token_reads_four_sources_in_documented_order() -> None:
    """`startToken()`: `start_param` → `tgWebAppStartParam` → `startapp` → `t`."""
    body = page_block("function startToken()", "function telegramInitData()")
    order = [
        "tg.initDataUnsafe.start_param",
        "queryParam('tgWebAppStartParam')",
        "queryParam('startapp')",
        "queryParam('t')",
    ]
    positions = [body.index(needle) for needle in order]
    assert positions == sorted(positions), positions

    # Query читается в одном безопасном месте: исключения не выходят наружу.
    query_body = page_block("function queryParam(name)", "function startToken()")
    assert "try {" in query_body and "catch (err)" in query_body


@requires_node
@pytest.mark.parametrize(
    ("search", "tg_js", "expected"),
    [
        # Каждый query-источник по отдельности (мобильный WebView и web-ссылки).
        ("?tgWebAppStartParam=476321_sig", "{}", "476321_sig"),
        ("?startapp=476321_sig", "{}", "476321_sig"),
        ("?t=476321_sig", "{}", "476321_sig"),
        # Порядок: tgWebAppStartParam → startapp → t.
        ("?t=debug&startapp=from_startapp", "{}", "from_startapp"),
        ("?t=debug&startapp=x&tgWebAppStartParam=from_query", "{}", "from_query"),
        ("?tgWebAppStartParam=from_query&startapp=from_startapp", "{}", "from_query"),
        # `start_param` из SDK важнее любого query; пустой/пробельный — не считается.
        ("?tgWebAppStartParam=from_query", '{initDataUnsafe: {start_param: "from_sdk"}}', "from_sdk"),
        ("?startapp=from_startapp", '{initDataUnsafe: {start_param: "  "}}', "from_startapp"),
        # Пробелы вокруг значения обрезаются.
        ("?tgWebAppStartParam=%20476321_sig%20", "{}", "476321_sig"),
        # Ни одного источника — пустая строка (а не исключение).
        ("", "{}", ""),
        ("?other=1", "{}", ""),
        ("?tgWebAppStartParam=&startapp=&t=", "{}", ""),
    ],
)
def test_start_token_returns_first_non_empty_source(
    search: str, tg_js: str, expected: str
) -> None:
    """Номер берётся из первого непустого источника, иначе — пустая строка."""
    assert run_start_token(search, tg_js) == expected


@requires_node
def test_start_token_never_throws_without_location_or_urlsearchparams() -> None:
    """Нет `window.location` или `URLSearchParams` — пустая строка, без исключений."""
    assert run_start_token(window_js="{}") == ""
    assert run_start_token(prelude="var URLSearchParams = null;\n") == ""
    assert (
        run_start_token(
            tg_js='{initDataUnsafe: {start_param: "sdk"}}',
            prelude="var URLSearchParams = null;\n",
        )
        == "sdk"
    )


def test_page_report_without_token_only_for_token_where() -> None:
    """`report()` без токена отправляет только `where='token'` (узкий контракт)."""
    reporter = page_block(
        "function report(level, message, where) {", "window.addEventListener('error'"
    )
    assert "if (!token && place !== 'token') return;" in reporter
    assert "if (!token) return;" not in reporter


def test_page_missing_token_is_reported_before_error_screen() -> None:
    """Ветка «нет токена»: сигнал в лог до экрана «Откройте сравнение из чата»."""
    load_body = page_block(
        "function load()",
        "// ------------------------------------------------------------- канвас/кадры",
    )
    assert re.search(
        r"if \(!token\) \{\s*report\('warning', 'нет токена', 'token'\);\s*showScreen\(",
        load_body,
    )


@requires_node
def test_report_drives_tokenless_beacon_and_keeps_token_reports() -> None:
    """Без токена уходит только сигнал «нет токена»; с токеном — обычные отчёты."""
    token_js = page_block("function queryParam(name)", "function telegramInitData()")
    reporter = page_block("var reported = new Set();", "window.addEventListener('error'")

    def calls_for(search: str) -> list[dict]:
        script = (
            "var calls = [];\n"
            "var tg = {};\n"
            "var window = {location: {search: "
            + json.dumps(search)
            + "}, fetch: function (url, opts) { calls.push(opts.body);"
            " return Promise.resolve(); }};\n"
            + token_js
            + reporter
            + "\nreport('warning', 'нет токена', 'token');\n"
            "report('error', 'boom', 'load');\n"
            "report('warning', 'нет токена', 'token');\n"
            "process.stdout.write(JSON.stringify(calls));"
        )
        return [json.loads(item) for item in json.loads(run_page_js(script))]

    no_token = calls_for("")
    assert no_token == [
        {
            "token": "",
            "level": "warning",
            "message": "нет токена",
            "where": "token",
        }
    ]

    # С токеном сигнал и обычный отчёт уходят, повторный сигнал — уже нет (dedupe).
    with_token = calls_for("?t=debug")
    assert [call["where"] for call in with_token] == ["token", "load"]
    assert {call["token"] for call in with_token} == {"debug"}


# ---------------------------------------------------------- первый кадр/отрисовка


def test_page_renders_through_workspace_and_calls_only_declared_functions() -> None:
    """Точка отрисовки — `workspace()`; необъявленная `layout()` не вызывается.

    Регрессия: `layout()` никогда не была объявлена, `ReferenceError` летел уже
    после `hideScreen()`, и пользователь видел пустую страницу вместо шторки.
    """
    assert "layout(" not in PAGE
    assert "function workspace()" in PAGE
    assert "function draw()" in PAGE

    script = PAGE.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    code = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", script)
    code = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', code)
    code = re.sub(r"//[^\n]*", "", code)

    declared = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", code))
    declared |= {
        name.strip().split("=")[0].strip()
        for params in re.findall(r"function\s*[A-Za-z_$\w]*\s*\(([^)]*)\)", code)
        for name in params.split(",")
        if name.strip()
    }
    called = set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", code))
    browser_globals = {
        "Error",
        "Image",
        "Map",
        "Number",
        "Promise",
        "ResizeObserver",
        "Set",
        "String",
        "URLSearchParams",
        "clearTimeout",
        "setTimeout",
        "fetch",
        "encodeURIComponent",
        # Ключевые слова и конструкции, которые regex принимает за вызов.
        "catch",
        "do",
        "else",
        "for",
        "function",
        "if",
        "new",
        "return",
        "switch",
        "throw",
        "try",
        "typeof",
        "var",
        "while",
    }
    assert called - declared - browser_globals == set()


def test_page_draws_first_frame_after_load_inside_try() -> None:
    """После загрузки данных кадр рисуется в `try`, а падение уходит в отчёт.

    `hideScreen()` и `workspace()` идут в одном `try`: если отрисовка упала, экран
    загрузки не остаётся скрытым — показывается `retry` с сообщением, а ошибка
    уходит в `/api/cmp/log` с `where='render'`. Исключение не выбрасывается
    дальше, чтобы не попасть в ветку «Нет связи».
    """
    load_body = page_block(
        "function load()", "// ------------------------------------------------------------- канвас/кадры"
    )
    assert re.search(r"try\s*\{\s*hideScreen\(\);\s*workspace\(\);", load_body), (
        "после загрузки данных нужен `hideScreen(); workspace();`"
    )
    render_catch = re.search(r"catch\s*\(err\)\s*\{([^}]*)\}", load_body)
    assert render_catch is not None, "падение отрисовки должно ловиться"
    assert "report('error', err, 'render')" in render_catch.group(1)
    assert re.search(r"showScreen\(\s*'retry',\s*'Не удалось отрисовать'", load_body)
    assert "throw" not in render_catch.group(1)


def test_page_redraws_on_resize_and_theme_change() -> None:
    """Кадр перерисовывается при ресайзе и смене темы, а не только один раз."""
    on_resize = page_block("function onResize()", "window.addEventListener('resize', onResize)")
    assert "workspace()" in on_resize
    assert "draw()" in PAGE  # workspace() -> draw()
    assert "window.addEventListener('resize', onResize)" in PAGE
    assert "window.addEventListener('orientationchange', onResize)" in PAGE
    assert "new ResizeObserver(onResize)" in PAGE

    apply_theme = page_block(
        "function applyTheme()", "// ----------------------------------------------------------- экраны статуса"
    )
    assert "draw()" in apply_theme
    assert "tg.onEvent('themeChanged', applyTheme)" in PAGE


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
        # Сравниваем текст, а не байты: сервер отдаёт файл как есть, а переводы
        # строк в рабочей копии зависят от core.autocrlf/gitattributes (на Windows
        # файл может оказаться с CRLF). Для содержимого страницы это не важно.
        assert body.replace("\r\n", "\n") == PAGE
        assert '<meta charset="utf-8">' in body
        for needle in (
            TELEGRAM_SCRIPT,
            "Telegram.WebApp",
            "/api/cmp/",
            "start_param",
            "<canvas",
            "themeParams",
        ):
            assert needle in body, needle


async def test_page_keeps_api_404_without_static_dir(tmp_path: Path) -> None:
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
