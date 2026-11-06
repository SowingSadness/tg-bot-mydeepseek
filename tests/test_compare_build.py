"""Тесты сборки бабла: шаблон, лимиты, подгонка по метрике и CLI. Без сети."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from bot.compare import build as build_module
from bot.compare.build import (
    RAW_LIMIT,
    Z_LIMIT,
    Attempt,
    Built,
    WidgetTooLarge,
    _aspect_pair,
    _height_for,
    _qualities,
    _widths,
    build,
    demo_pair,
    main,
    render,
    render_cached,
    zsize,
)
from bot.compare.images import PROFILES, ImageError, fit_box, to_webp

LABELS = ("До", "После")
TITLE = "Сравнение"
HINT = "Проведите ↔"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "bot" / "compare" / "template.html"


def _template_text() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """Тело правила CSS для точного селектора (`#k` не должен матчить `#k img`).

    В шаблоне одно правило на строку, поэтому тело — это остаток строки без `}`.
    """
    match = re.search(r"^" + re.escape(selector) + r"\s*\{(.*)$", css, re.MULTILINE)
    assert match, f"в шаблоне нет правила {selector}"
    body = match.group(1)
    assert body.endswith("}"), f"правило {selector} должно заканчиваться на своей строке"
    return body[:-1]


def _blue() -> Image.Image:
    return Image.new("RGB", (900, 700), (20, 60, 140))


def _orange() -> Image.Image:
    return Image.new("RGB", (1200, 800), (240, 150, 40))


def _noise(size: tuple[int, int] = (400, 300)) -> Image.Image:
    return Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))


def _layer_sizes(html: str) -> list[tuple[int, int]]:
    """Пары `width`/`height` атрибутов обеих картинок в порядке появления в HTML."""
    return [
        (int(width), int(height))
        for width, height in re.findall(r'<img [^>]*width="(\d+)" height="(\d+)"', html)
    ]


def _webp_size(blob: bytes) -> tuple[int, int]:
    """Реальный размер запечённого WebP: с ним обязаны совпадать атрибуты слоёв."""
    with Image.open(BytesIO(blob)) as decoded:
        return decoded.size


# --------------------------------------------------------------------------- render


def test_render_fills_every_placeholder():
    html = render("data:a", "data:b", labels=LABELS, title="Мой бабл", hint="Тяни")

    # `}}` встречается в минифицированном JS, поэтому ищем именно плейсхолдеры.
    assert not re.search(r"\{\{[A-Z_]+\}\}", html)
    assert "Мой бабл" in html
    assert "aspect-ratio: 3 / 2" in html
    assert "До" in html and "После" in html
    assert "Тяни" in html
    assert "data:a" in html and "data:b" in html


def test_render_uses_custom_aspect():
    html = render("a", "b", labels=LABELS, title=TITLE, hint=HINT, aspect="4:3")
    assert "aspect-ratio: 4 / 3" in html


def test_render_bakes_layer_size_attributes():
    html = render("a", "b", labels=LABELS, title=TITLE, hint=HINT, aspect="4:3", width=300)

    sizes = _layer_sizes(html)
    assert sizes == [(300, 225), (300, 225)]
    assert "{{SIZE}}" not in html
    assert 'src="a" width="300" height="225"' in html
    assert 'src="b" width="300" height="225"' in html


def test_render_without_width_has_no_size_attributes():
    html = render("a", "b", labels=LABELS, title=TITLE, hint=HINT)

    assert "{{SIZE}}" not in html
    assert 'width="' not in html and 'height="' not in html


def test_render_height_matches_webp_size_for_every_aspect():
    """Атрибуты и запечённый WebP считаются одной формулой — иначе слои разъедутся."""
    img = _blue()
    for aspect in ("3:2", "4:3", "16:9"):
        _, blob, _ = to_webp(img, 203, 30, aspect=tuple(_aspect_pair(aspect)))
        html = render("a", "b", labels=LABELS, title=TITLE, hint=HINT, aspect=aspect, width=203)

        expected = _webp_size(blob)
        assert _layer_sizes(html) == [expected, expected], aspect
        assert expected[0] == 203
        assert expected[1] == _height_for(203, aspect)


def test_aspect_pair_accepts_integers_and_fractions():
    assert _aspect_pair("3:2") == (3, 2)
    assert _aspect_pair("4:3") == (4, 3)
    assert _aspect_pair("16:9") == (16, 9)
    assert _aspect_pair("2.66:1") == (133, 50)
    assert _aspect_pair((3, 2)) == (3, 2)

    with pytest.raises(ImageError):
        _aspect_pair("32")
    with pytest.raises(ImageError):
        _aspect_pair((0, 2))


def test_render_keeps_css_braces():
    """str.format сломал бы CSS — проверяем, что фигурные скобки на месте."""
    html = render("a", "b", labels=LABELS, title=TITLE, hint=HINT)
    assert "<style>" in html and "</style>" in html
    assert "body{margin:0" in html
    assert "#c{position:relative" in html


def test_render_escapes_labels():
    html = render("a", "b", labels=("<A>", '&"B"'), title="T&T", hint="h")
    assert "&lt;A&gt;" in html
    assert "<title>T&amp;T</title>" in html
    assert "&quot;B&quot;" in html


def test_render_cached_matches_render():
    url_a, blob_a, _ = to_webp(_blue(), 300, 30)
    url_b, blob_b, _ = to_webp(_orange(), 300, 30)

    fresh = render(url_a, url_b, labels=LABELS, title=TITLE, hint=HINT)
    cached = render_cached(
        image_a=blob_a, image_b=blob_b, labels=LABELS, title=TITLE, hint=HINT
    )

    assert cached == fresh
    assert "{{" not in cached


def test_render_cached_defaults_to_3_2_and_honours_aspect():
    _, blob_a, _ = to_webp(_blue(), 150, 30)
    _, blob_b, _ = to_webp(_orange(), 150, 30)
    base = dict(image_a=blob_a, image_b=blob_b, labels=LABELS, title=TITLE, hint=HINT)

    assert "aspect-ratio: 3 / 2" in render_cached(**base)
    assert "aspect-ratio: 4 / 3" in render_cached(**base, aspect="4:3")


def test_render_cached_bakes_size_only_when_width_given():
    _, blob_a, _ = to_webp(_blue(), 150, 30)
    _, blob_b, _ = to_webp(_orange(), 150, 30)
    base = dict(image_a=blob_a, image_b=blob_b, labels=LABELS, title=TITLE, hint=HINT)

    assert _layer_sizes(render_cached(**base)) == []
    assert _layer_sizes(render_cached(**base, width=150)) == [(150, 100), (150, 100)]
    assert _layer_sizes(render_cached(**base, aspect="16:9", width=150)) == [
        (150, 84),
        (150, 84),
    ]


def test_zsize_matches_zlib_level_9():
    html = render("a", "b", labels=LABELS, title=TITLE, hint=HINT)
    assert zsize(html) == len(zlib.compress(html.encode("utf-8"), 9))


# ------------------------------------------------------------------- шаблон


def test_template_has_no_division_or_pn_variable():
    """Якорь: ширина клипа не делится на позицию и не зависит от `--pn`."""
    template = _template_text()

    assert "calc(100% /" not in template
    assert "var(--pn)" not in template
    assert "--pn" not in template


def test_template_keeps_clip_anchored_to_left_edge():
    template = _template_text()
    clip = _rule(template, "#k")
    image = _rule(template, "#k img")

    # Верхний слой: без `right`/`inset`, иначе раскладка переопределена.
    for declaration in ("left:0", "top:0", "bottom:0"):
        assert declaration in clip
    assert "right:" not in clip
    assert "inset" not in clip
    assert "calc(var(--p)*1%)" in clip

    # Картинка внутри клипа: размеры задаются атрибутами и JS в px, не из пропорций.
    for declaration in ("left:0", "top:0", "height:100%", "max-width:none"):
        assert declaration in image
    assert "width:auto" not in image
    assert "object-fit" not in image
    assert "right:" not in image
    assert "inset" not in image


def test_template_bakes_size_and_syncs_layers_from_js():
    """Слои получают запечённые атрибуты и px-размеры из `#c`, а не из `width:auto`."""
    template = _template_text()

    assert template.count("{{SIZE}}") == 2
    assert '<img id=ib src="{{B}}" {{SIZE}}>' in template
    assert '<div id=k><img id=ia src="{{A}}" {{SIZE}}></div>' in template

    # Размер обновляется после layout’а, в кадре анимации и на каждом касании.
    assert "function sz(){var b=c.getBoundingClientRect()" in template
    assert "ib.style.width=ia.style.width=b.width+'px'" in template
    assert "ib.style.height=ia.style.height=b.height+'px'" in template
    assert "f();sz();requestAnimationFrame(sz);" in template
    assert "if(!(b.width>0))return;\nsz();" in template


def test_template_storage_access_is_guarded_by_try():
    """Запрещённый localStorage (приватный режим/песочница) не должен ломать IIFE."""
    template = _template_text()

    for needle in ("localStorage", "L.setItem"):
        positions = [match.start() for match in re.finditer(re.escape(needle), template)]
        assert positions, f"в шаблоне нет {needle}"
        for position in positions:
            assert "try" in _open_blocks(template, position), (
                f"{needle} вне try (позиция {position}): падение storage убьёт шторку"
            )

    # Без storage остаётся рабочее значение по умолчанию.
    assert "L=null" in template
    assert "p=.5" in template


def _open_blocks(js: str, position: int) -> list[str]:
    """Какие блоки открыты перед `position` (для проверки «внутри try»)."""
    stack: list[str] = []
    index = 0
    while index < position:
        if js.startswith("try{", index):
            stack.append("try")
            index += 4
            continue
        char = js[index]
        if char == "{":
            stack.append("block")
        elif char == "}" and stack:
            stack.pop()
        index += 1
    return stack


def test_template_bottom_layer_covers_bubble():
    template = _template_text()
    base = _rule(template, "#ib")

    for declaration in ("left:0", "top:0", "width:100%", "height:100%", "object-fit:cover"):
        assert declaration in base


def test_template_bubble_keeps_aspect_and_vertical_scroll():
    bubble = _rule(_template_text(), "#c")

    assert "aspect-ratio: {{AR}}" in bubble
    assert "touch-action: pan-y" in bubble
    assert "overflow:hidden" in bubble


def test_template_position_variable_is_percent():
    """`--p` хранится в процентах: иначе `calc(var(--p)*1%)` даёт 0.5% вместо 50%."""
    template = _template_text()

    assert "--p:50" in _rule(template, "#c")
    assert "c.style.setProperty('--p',p*100)" in template
    assert "calc(var(--p)*1%)" in _rule(template, "#k")
    assert "calc(var(--p)*1%)" in _rule(template, "#b")


def test_template_fits_minified_budget():
    """Шаблон сам по себе должен занимать < 1100 Б zlib из бюджета виджета.

    Было 994 Б до явных px-размеров слоёв; `sz()` и `{{SIZE}}` добавили ~80 Б.
    """
    assert zsize(_template_text()) <= 1100


# --------------------------------------------------------------------------- build


def test_build_demo_pair_fits_limits():
    img_a, img_b = demo_pair()
    built = build(img_a, img_b, labels=LABELS, title=TITLE, hint=HINT)

    assert isinstance(built, Built)
    assert built.html_bytes <= RAW_LIMIT
    assert built.zlib_bytes <= Z_LIMIT - 64
    assert built.image_a[:4] == b"RIFF" and built.image_b[:4] == b"RIFF"
    assert built.width in _widths(430, 160)
    assert built.quality_a in _qualities(34, 14)
    assert built.quality_b in _qualities(34, 14)
    assert built.quality == max(built.quality_a, built.quality_b)
    assert built.profile_a in PROFILES and built.profile_b in PROFILES
    assert built.aspect == "3:2"
    assert 0.0 <= built.err_a <= 1.0
    assert 0.0 <= built.err_b <= 1.0
    assert built.html == render_cached(
        image_a=built.image_a,
        image_b=built.image_b,
        labels=LABELS,
        title=TITLE,
        hint=HINT,
        aspect=built.aspect,
        width=built.width,
    )
    assert "aspect-ratio: 3 / 2" in built.html
    assert "{{SIZE}}" not in built.html
    assert "{{" not in built.html

    # Запечённые атрибуты обеих картинок совпадают с реальным размером WebP.
    sizes = _layer_sizes(built.html)
    assert sizes == [_webp_size(built.image_a), _webp_size(built.image_b)]
    assert sizes == [(built.width, _height_for(built.width, built.aspect))] * 2


def test_build_honours_aspect_in_size_attributes():
    img_a, img_b = demo_pair()
    built = build(
        img_a,
        img_b,
        start_width=200,
        min_width=160,
        labels=LABELS,
        title=TITLE,
        hint=HINT,
        aspect=(4, 3),
    )

    assert built.aspect == "4:3"
    sizes = _layer_sizes(built.html)
    assert sizes == [_webp_size(built.image_a), _webp_size(built.image_b)]
    assert sizes == [(built.width, _height_for(built.width, "4:3"))] * 2


def test_build_uses_one_crop_box_for_both_images(monkeypatch):
    seen: list[tuple[bool, tuple[float, float, float, float]]] = []
    original = build_module.to_webp

    def spy(img, width, quality, **kwargs):
        seen.append((img is img_a, kwargs["box"]))
        return original(img, width, quality, **kwargs)

    img_a = Image.new("RGB", (2000, 800), (20, 60, 140))
    img_b = Image.new("RGB", (1200, 800), (240, 150, 40))
    monkeypatch.setattr(build_module, "to_webp", spy)

    built = build(img_a, img_b, labels=LABELS, title=TITLE, hint=HINT)

    assert built.aspect == "3:2"
    boxes = {box for _, box in seen}
    assert boxes == {fit_box(img_a, (3, 2))}
    assert {is_a for is_a, _ in seen} == {True, False}


def test_build_prefers_lowest_worst_error_among_fitting(monkeypatch):
    """При урезанном бюджете влезают не все пары — выбирается лучшая по max(err)."""
    report: list[Attempt] = []
    img_a, img_b = demo_pair()

    built = build(
        img_a,
        img_b,
        start_quality=42,
        min_quality=10,
        margin=300,  # бюджет меньше, чем нужно самым качественным парам
        labels=LABELS,
        title=TITLE,
        hint=HINT,
        report=report,
    )

    fitting = [row for row in report if row.fits and row.width == built.width]
    assert len(fitting) > 1
    assert any(not row.fits for row in report)
    assert built.quality_a in _qualities(42, 10)
    assert built.quality_b in _qualities(42, 10)
    chosen = next(
        row
        for row in fitting
        if row.quality_a == built.quality_a
        and row.quality_b == built.quality_b
        and row.zlib_bytes == built.zlib_bytes
    )
    best = min(row.worst_err for row in fitting)
    assert chosen.worst_err == pytest.approx(best)
    assert max(built.err_a, built.err_b) == pytest.approx(best)


def test_build_raises_on_noise():
    img_a, img_b = _noise(), _noise()

    with pytest.raises(WidgetTooLarge) as exc:
        build(img_a, img_b, min_width=400, min_quality=60, labels=LABELS, title=TITLE, hint=HINT)

    message = str(exc.value)
    assert "400px" in message
    assert "B HTML" in message and "B zlib" in message


def test_build_is_bounded_by_encode_limit(monkeypatch):
    """Лимит кодирований не даёт «сканировать всё слепо» на пёстрых картинках."""
    monkeypatch.setattr(build_module, "MAX_ENCODES", 6)
    report: list[Attempt] = []

    with pytest.raises(WidgetTooLarge):
        build(
            _noise(),
            _noise(),
            start_width=800,
            min_width=100,
            min_quality=60,
            labels=LABELS,
            title=TITLE,
            hint=HINT,
            report=report,
        )

    # 6 энкодов = 3 кандидата на каждую картинку → пары только на одной ширине.
    assert {row.width for row in report} == {800}


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
    assert "aspect=3:2" in out
    assert target.is_file()

    assert main(["--check", str(target)]) == 0
    check_out = capsys.readouterr().out
    assert "raw=" in check_out and "zlib=" in check_out


def test_cli_report_prints_table_winner_and_time(workdir, capsys):
    target = workdir / "report.html"

    assert main(["--demo", "--out", str(target), "--report"]) == 0
    out = capsys.readouterr().out

    assert "width" in out and "err_a" in out and "err_b" in out
    assert "ВЫБРАН:" in out
    assert "время сборки:" in out
    assert target.is_file()
    assert main(["--check", str(target)]) == 0


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

    assert main(["--a", str(a), "--b", str(b), "--out", str(out), "--report"]) == 0
    out_text = capsys.readouterr().out
    assert "width=" in out_text and "ВЫБРАН:" in out_text
    assert main(["--check", str(out)]) == 0


def test_cli_reports_bad_image(workdir, capsys):
    bad = workdir / "a.png"
    bad.write_bytes(b"not an image")
    good = workdir / "b.png"
    good.write_bytes(b"also not an image")

    assert main(["--a", str(bad), "--b", str(good)]) == 2
    assert "не подошла" in capsys.readouterr().err.lower()
