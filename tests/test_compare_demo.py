"""Тесты локальной демки бабла: файлы, экранирование, мок и сервер. Без сети и браузера."""

from __future__ import annotations

import functools
import html as html_module
import re
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from bot.compare import build as build_module
from bot.compare import demo, images


def _picture(path: Path, color: tuple[int, int, int], accent: tuple[int, int, int]) -> Path:
    """Простая гладкая картинка: сборка на ней быстрая и предсказуемая."""
    img = Image.new("RGB", (360, 240), color)
    ImageDraw.Draw(img).ellipse((60, 40, 300, 200), fill=accent)
    img.save(path, "PNG")
    return path


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("demo-src")
    return (
        _picture(root / "a.png", (20, 60, 140), (240, 150, 40)),
        _picture(root / "b.png", (40, 120, 90), (250, 250, 120)),
    )


@pytest.fixture(scope="module")
def built(sources) -> build_module.Built:
    return demo.build_widget(
        images.load(sources[0].read_bytes()),
        images.load(sources[1].read_bytes()),
    )


@pytest.fixture(scope="module")
def page_dir(tmp_path_factory, sources) -> Path:
    """`--no-server`: только файлы, как при обычном запуске демки."""
    out = tmp_path_factory.mktemp("demo-out") / "nested"
    code = demo.main(
        [
            "--a",
            str(sources[0]),
            "--b",
            str(sources[1]),
            "--out",
            str(out),
            "--no-server",
        ]
    )
    assert code == 0
    return out


def _page(path: Path) -> str:
    return (path / "index.html").read_text(encoding="utf-8")


def _srcdoc(page: str) -> str:
    """Сырое значение атрибута `srcdoc` — кавычки внутри не должны его рвать."""
    match = re.search(r'srcdoc="([^"]*)"', page)
    assert match, "в index.html нет srcdoc"
    return match.group(1)


# ------------------------------------------------------------------------ файлы


def test_cli_writes_exactly_widget_and_index(page_dir: Path) -> None:
    assert sorted(item.name for item in page_dir.iterdir()) == ["index.html", "widget.html"]
    assert (page_dir / "widget.html").stat().st_size > 0
    assert (page_dir / "index.html").stat().st_size > 0


def test_widget_is_exact_sendwidget_payload(page_dir: Path, sources) -> None:
    expected = build_module.build(
        images.load(sources[0].read_bytes()),
        images.load(sources[1].read_bytes()),
        labels=demo.LABELS,
        **demo.BUILD_OPTIONS,
    )
    assert (page_dir / "widget.html").read_text(encoding="utf-8") == expected.html
    # И байт в байт: никакого CRLF, иначе файл ≠ payload для sendWidget.
    assert (page_dir / "widget.html").read_bytes() == expected.html.encode("utf-8")
    assert (page_dir / "widget.html").stat().st_size == expected.html_bytes


def test_index_has_mock_iframe_theme_and_slider(page_dir: Path) -> None:
    page = _page(page_dir)
    assert "TelegramWidget" in page
    assert "<iframe" in page
    for variable in (
        "--tg-theme-bg-color",
        "--tg-theme-text-color",
        "--tg-theme-hint-color",
        "--tg-theme-link-color",
        "--tg-theme-button-color",
        "--tg-theme-button-text-color",
        "--tg-theme-secondary-bg-color",
        "--tg-theme-text-padding",
    ):
        assert variable in page
    assert 'min="200"' in page and 'max="600"' in page
    assert 'type="range"' in page
    assert "srcdoc=" in page  # без сервера страница самодостаточна


# ------------------------------------------------------------------- высота / AR


def test_iframe_height_follows_aspect(built) -> None:
    for width, height in [(200, 133), (320, 213), (430, 287), (600, 400)]:
        page = demo.index_html(built, width=width)
        assert demo.aspect_ratio(built.aspect) == pytest.approx(3 / 2)
        assert f'height="{height}"' in page, width
        assert f"height:{height}px" in page, width
        # JS пересчитывает ту же формулу при смене ширины.
        assert "Math.round(width / AR)" in page


def test_serve_mode_uses_widget_url(built) -> None:
    page = demo.index_html(built, width=430, serve=True)
    assert 'src="widget.html"' in page
    assert "srcdoc=" not in page


# ---------------------------------------------------------------- экранирование


def test_srcdoc_escapes_mock_and_widget(page_dir: Path, built) -> None:
    page = _page(page_dir)
    value = _srcdoc(page)

    assert "</script>" not in value, "закрывающий тег не должен рвать srcdoc"
    assert "&lt;/script&gt;" in value
    assert '"' not in value, "кавычки должны быть экранированы"
    # После закрывающей кавычки атрибута идёт `>`: атрибут не разорван.
    match = re.search(r'srcdoc="([^"]*)"', page)
    assert match is not None
    assert page[match.end()] == ">"
    # Раскодированный srcdoc — это ровно мок + виджет (ничего не потеряно).
    assert html_module.unescape(value) == demo.with_mock(built.html)

    # Враждебные символы в бабле не рвут ни `srcdoc`, ни JS-строку.
    weird = '<script>alert("x")</script><img src="x">&'
    dangerous = build_module.Built(
        html=weird,
        width=430,
        quality=34,
        zlib_bytes=1,
        html_bytes=len(weird),
        picture_bytes=1,
        image_a=b"",
        image_b=b"",
    )
    page = demo.index_html(dangerous, width=430)
    value = _srcdoc(page)
    assert "</script>" not in value
    assert '"' not in value
    assert html_module.unescape(value) == demo.with_mock(weird)
    assert demo._js_string("</script>") == '"<\\/script>"'


def test_mock_goes_before_widget() -> None:
    served = demo.with_mock("<p>widget</p>")
    assert served.startswith("<script>")
    assert served.endswith("<p>widget</p>")
    assert "window.TelegramWidget" in served
    assert "__tgSetTheme" in served
    assert served.index("<script>") < served.index("<p>widget</p>")
    for member in (
        "themeParams",
        "colorScheme",
        "HapticFeedback",
        "contentSafeAreaInset",
        "request:",
        "ready:",
        "onEvent:",
    ):
        assert member in served


# ------------------------------------------------------------------ CLI-опции


def test_labels_land_in_widget_and_page(tmp_path: Path, sources) -> None:
    out = tmp_path / "labels"
    code = demo.main(
        [
            "--a",
            str(sources[0]),
            "--b",
            str(sources[1]),
            "--out",
            str(out),
            "--labels",
            "A|B",
            "--no-server",
        ]
    )
    assert code == 0
    widget = (out / "widget.html").read_text(encoding="utf-8")
    assert ">A<" in widget and ">B<" in widget
    inside = html_module.unescape(_srcdoc(_page(out)))
    assert ">A<" in inside and ">B<" in inside


def test_no_server_does_not_bind(monkeypatch, tmp_path: Path, sources) -> None:
    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("без --serve сервер подниматься не должен")

    monkeypatch.setattr(demo, "make_server", forbidden)
    monkeypatch.setattr(demo, "bind_server", forbidden)

    code = demo.main(
        [
            "--a",
            str(sources[0]),
            "--b",
            str(sources[1]),
            "--out",
            str(tmp_path / "no-server"),
            "--no-server",
        ]
    )
    assert code == 0
    assert (tmp_path / "no-server" / "index.html").exists()


def test_widget_too_large_returns_2(monkeypatch, tmp_path: Path, sources) -> None:
    def too_large(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise build_module.WidgetTooLarge("даже 160px q12 не влезает")

    monkeypatch.setattr(demo, "build_widget", too_large)
    out = tmp_path / "too-large"
    code = demo.main(["--a", str(sources[0]), "--b", str(sources[1]), "--out", str(out)])
    assert code == 2
    assert not out.exists(), "при WidgetTooLarge файлы не пишутся"


# --------------------------------------------------------------------- сервер


def test_server_factory_builds_handler(tmp_path: Path) -> None:
    server = demo.make_server(tmp_path, 0)
    try:
        assert isinstance(server, demo.ThreadingHTTPServer)
        assert server.server_address[0] == "127.0.0.1"
        handler = server.RequestHandlerClass
        assert isinstance(handler, functools.partial)
        assert handler.func is demo.DemoRequestHandler
        assert handler.keywords["directory"] == str(tmp_path)
    finally:
        server.server_close()


class _FakeServer:
    """Подмена настоящего сервера: проверяем печать адреса и остановку по Ctrl+C."""

    def __init__(self) -> None:
        self.server_address = ("127.0.0.1", 8765)
        self.closed = False

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


def test_serve_prints_url_stops_and_handles_ports(monkeypatch, capsys, tmp_path: Path) -> None:
    fake = _FakeServer()
    opened: list[str] = []
    used: list[int] = []

    def factory(directory, port=0):  # noqa: ANN001, ANN202
        used.append(port)
        return fake

    monkeypatch.setattr(demo, "make_server", factory)
    monkeypatch.setattr(demo, "bind_server", lambda directory: fake)
    monkeypatch.setattr(demo.webbrowser, "open", opened.append)

    assert demo._serve(tmp_path, port=None, open_browser=True) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8765/" in out
    assert "Ctrl+C" in out
    assert fake.closed
    assert opened == ["http://127.0.0.1:8765/"]

    assert demo._serve(tmp_path, port=9000, open_browser=False) == 0
    assert used == [9000]

    def busy(directory, port=0):  # noqa: ANN001, ANN202, ARG001
        raise OSError("порт занят")

    monkeypatch.setattr(demo, "make_server", busy)
    assert demo._serve(tmp_path, port=8765, open_browser=False) == 1


# --------------------------------------------------------------------- хелперы


def test_aspect_ratio_labels_clamp_and_caption(built) -> None:
    assert demo.aspect_ratio("3:2") == pytest.approx(1.5)
    assert demo.aspect_ratio("4:3") == pytest.approx(4 / 3)
    assert demo.aspect_ratio("мусор") == pytest.approx(1.5)
    assert demo.aspect_ratio("0:2") == pytest.approx(1.5)

    assert demo.parse_labels("A|B") == ("A", "B")
    assert demo.parse_labels(" До | После ") == ("До", "После")
    assert demo.parse_labels("без разделителя") == demo.LABELS
    assert demo.parse_labels(None) == demo.LABELS

    assert demo.clamp_width(320) == 320
    assert demo.clamp_width(100) == demo.MIN_WIDTH
    assert demo.clamp_width(900) == demo.MAX_WIDTH

    caption = demo.caption(built, 320)
    assert caption.startswith("320px · zlib ")
    assert f"/{build_module.Z_LIMIT}" in caption
    assert f"q{built.quality_a}/{built.quality_b}" in caption
    assert f"{built.profile_a}/{built.profile_b}" in caption

    details = demo.details(built)
    assert "err_a" in details and "err_b" in details
    assert f"raw {built.html_bytes} B" in details
