"""Тесты сборки бабла: шаблон, лимиты, подгонка и CLI. Без сети."""

from __future__ import annotations

import os
import shutil
import tempfile
import zlib
from pathlib import Path

import pytest
from PIL import Image

from bot.compare.build import (
    RAW_LIMIT,
    Z_LIMIT,
    Built,
    WidgetTooLarge,
    _qualities,
    _widths,
    build,
    demo_pair,
    main,
    render,
    render_cached,
    zsize,
)
from bot.compare.images import to_webp

LABELS = ("До", "После")
TITLE = "Сравнение"
HINT = "Проведите ↔"


def _blue() -> Image.Image:
    return Image.new("RGB", (900, 700), (20, 60, 140))


def _orange() -> Image.Image:
    return Image.new("RGB", (1200, 800), (240, 150, 40))


def _noise(size: tuple[int, int] = (400, 300)) -> Image.Image:
    return Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))


# --------------------------------------------------------------------------- render


def test_render_fills_every_placeholder():
    html = render("data:a", "data:b", labels=LABELS, title="Мой бабл", hint="Тяни")

    assert "{{" not in html and "}}" not in html
    assert "Мой бабл" in html
    assert "aspect-ratio: 4 / 3" in html
    assert "До" in html and "После" in html
    assert "Тяни" in html
    assert "data:a" in html and "data:b" in html


def test_render_keeps_css_braces():
    """str.format сломал бы CSS — проверяем, что фигурные скобки на месте."""
    html = render("a", "b", labels=LABELS, title=TITLE, hint=HINT)
    assert "body { margin: 0;" in html
    assert "--tg-theme-text-color" in html


def test_render_escapes_labels():
    html = render("a", "b", labels=("<A>", '&"B"'), title="T&T", hint="h")
    assert "&lt;A&gt;" in html
    assert "<title>T&amp;T</title>" in html
    assert "&quot;B&quot;" in html


def test_render_cached_matches_render():
    url_a, blob_a = to_webp(_blue(), 200, 30)
    url_b, blob_b = to_webp(_orange(), 200, 30)

    fresh = render(url_a, url_b, labels=LABELS, title=TITLE, hint=HINT)
    cached = render_cached(
        image_a=blob_a, image_b=blob_b, labels=LABELS, title=TITLE, hint=HINT
    )

    assert cached == fresh
    assert "{{" not in cached


def test_zsize_matches_zlib_level_9():
    html = render("a", "b", labels=LABELS, title=TITLE, hint=HINT)
    assert zsize(html) == len(zlib.compress(html.encode("utf-8"), 9))


# --------------------------------------------------------------------------- build


def test_build_demo_pair_fits_limits():
    img_a, img_b = demo_pair()
    built = build(img_a, img_b, labels=LABELS, title=TITLE, hint=HINT)

    assert isinstance(built, Built)
    assert built.html_bytes <= RAW_LIMIT
    assert built.zlib_bytes <= Z_LIMIT - 64
    assert built.image_a[:4] == b"RIFF" and built.image_b[:4] == b"RIFF"
    assert (built.width, built.quality) in [
        (w, q) for w in _widths(430, 160) for q in _qualities(34, 16)
    ]
    assert built.html == render_cached(
        image_a=built.image_a, image_b=built.image_b, labels=LABELS, title=TITLE, hint=HINT
    )
    assert "{{" not in built.html


def test_build_raises_on_noise():
    img_a, img_b = _noise(), _noise()

    with pytest.raises(WidgetTooLarge) as exc:
        build(img_a, img_b, min_width=400, min_quality=60, labels=LABELS, title=TITLE, hint=HINT)

    message = str(exc.value)
    assert "400px" in message
    assert "B HTML" in message and "B zlib" in message


def test_widths_end_exactly_at_min_width():
    widths = _widths(430, 160)
    assert widths[0] == 430
    assert widths[-1] == 160
    assert widths == sorted(widths, reverse=True)
    assert len(set(widths)) == len(widths)


def test_qualities_step_by_four():
    assert _qualities(34, 16) == [34, 30, 26, 22, 18, 16]
    assert _qualities(20, 16) == [20, 16]
    assert _qualities(16, 16) == [16]


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def workdir():
    """Своя временная папка с уборкой: системный temp может указывать в репозиторий."""
    path = Path(tempfile.mkdtemp(prefix="cmp-build-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_cli_demo_writes_html_and_check_passes(workdir, capsys):
    target = workdir / "nested" / "cmp-demo.html"

    assert main(["--demo", "--out", str(target)]) == 0
    out = capsys.readouterr().out
    assert "width=" in out and "quality=" in out and "zlib=" in out and "raw=" in out
    assert target.is_file()

    assert main(["--check", str(target)]) == 0
    check_out = capsys.readouterr().out
    assert "raw=" in check_out and "zlib=" in check_out


def test_cli_check_fails_on_oversize_html(workdir):
    oversized = workdir / "big.html"
    oversized.write_text("x" * (RAW_LIMIT + 1), encoding="utf-8")

    assert main(["--check", str(oversized)]) == 1


def test_cli_requires_arguments(capsys):
    assert main([]) == 2
    assert "--demo" in capsys.readouterr().err


def test_cli_file_mode(workdir, capsys):
    from io import BytesIO

    def save(img: Image.Image, name: str):
        path = workdir / name
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        path.write_bytes(buffer.getvalue())
        return path

    a = save(_blue(), "a.png")
    b = save(_orange(), "b.png")
    out = workdir / "out.html"

    assert main(["--a", str(a), "--b", str(b), "--out", str(out)]) == 0
    assert "width=" in capsys.readouterr().out
    assert main(["--check", str(out)]) == 0


def test_cli_reports_bad_image(workdir, capsys):
    bad = workdir / "a.png"
    bad.write_bytes(b"not an image")
    good = workdir / "b.png"
    good.write_bytes(b"also not an image")

    assert main(["--a", str(bad), "--b", str(good)]) == 2
    assert "не подошла" in capsys.readouterr().err.lower()
