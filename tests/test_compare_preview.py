"""Тесты композита-превью «A|B»: размеры, разделитель, кроп, best-effort подписи."""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from bot.compare import images, preview


def solid(color: tuple[int, int, int], size: tuple[int, int] = (400, 200)) -> Image.Image:
    return Image.new("RGB", size, color)


def decode(blob: bytes) -> Image.Image:
    return Image.open(BytesIO(blob)).convert("RGB")


def test_render_returns_jpeg_with_computed_size():
    blob = preview.render(solid((200, 0, 0)), solid((0, 0, 200)), width=640)

    assert blob[:2] == b"\xff\xd8"  # JPEG-сигнатура
    assert blob[-2:] == b"\xff\xd9"  # EOI: файл целый
    image = Image.open(BytesIO(blob))
    assert image.format == "JPEG"
    # 640 → два кадра по 320, высота 3:2 → 213.
    assert image.size == (640, 213)
    assert image.size == preview.size_for(640)


def test_center_of_composite_has_white_divider_with_dark_outline():
    blob = preview.render(solid((230, 40, 40)), solid((40, 40, 230)), width=640)
    image = decode(blob)
    half = image.width // 2

    # Белая линия — на самом центре (допуск на JPEG-артефакты).
    center = image.getpixel((half, image.height // 2))
    assert min(center) >= 230, center
    # Внутри линии белым остаётся весь её хвост, а не один пиксель.
    for offset in (-2, -1, 0, 1, 2):
        pixel = image.getpixel((half + offset, image.height // 3))
        assert min(pixel) >= 225, (offset, pixel)
    # Обводка: сразу за линией тёмные пиксели, заметно темнее белой линии.
    for offset in (-5, 5):
        pixel = image.getpixel((half + offset, image.height // 3))
        assert max(pixel) <= 120, (offset, pixel)


def test_left_and_right_halves_show_their_own_image():
    blob = preview.render(solid((250, 10, 10)), solid((10, 10, 250)), width=640)
    image = decode(blob)
    half = image.width // 2
    left = image.getpixel((half // 2, image.height // 2))
    right = image.getpixel((half + half // 2, image.height // 2))

    assert left[0] > 200 and left[2] < 60, left
    assert right[2] > 200 and right[0] < 60, right
    assert image.getpixel((5, 5)) != image.getpixel((image.width - 6, 5))


def test_uses_same_crop_window_as_build():
    """Широкая картинка режется по `fit_box` первой картинки, как в `build.build`.

    Зелёная полоса слева (0..50 px) лежит ВНЕ окна 3:2 (400×200 → x 50..350),
    поэтому в композите её быть не должно: иначе превью не совпадёт со шторкой.
    """
    source = Image.new("RGB", (400, 200), (250, 10, 10))
    for x in range(0, 50):
        for y in range(0, 200):
            source.putpixel((x, y), (10, 250, 10))

    window = images.fit_box(source, (3, 2))
    assert window == pytest.approx((0.125, 0.0, 0.875, 1.0))

    blob = preview.render(source, solid((10, 10, 250), (400, 200)), width=640)
    image = decode(blob)

    for x in (0, 1, 2):
        pixel = image.getpixel((x, image.height // 2))
        assert pixel[0] > 200 and pixel[1] < 80, (x, pixel)


def test_render_without_any_font_still_returns_jpeg(monkeypatch):
    """Нет TTF — композит без подписей, но валидный JPEG (падений быть не должно)."""
    monkeypatch.setattr(preview, "FONT_CANDIDATES", ())

    blob = preview.render(solid((0, 120, 0)), solid((120, 0, 0)), width=320)

    assert blob[:2] == b"\xff\xd8"
    assert Image.open(BytesIO(blob)).size == (320, 107)


def test_render_tolerates_missing_or_broken_labels(monkeypatch):
    monkeypatch.setattr(preview, "FONT_CANDIDATES", ())

    for labels in (("", ""), (), None):
        blob = preview.render(solid((0, 0, 0)), solid((255, 255, 255)), width=64, labels=labels)
        assert blob[:2] == b"\xff\xd8"


def test_aspect_variants_change_only_height():
    blob = preview.render(solid((9, 9, 9)), solid((200, 200, 200)), width=640, aspect="4:3")

    assert Image.open(BytesIO(blob)).size == (640, round(320 * 3 / 4))
    assert preview.size_for(1000) == (1000, round(500 * 2 / 3))
    assert preview.size_for(1000, "4:3") == (1000, round(500 * 3 / 4))


def test_invalid_width_and_aspect_raise_image_error():
    with pytest.raises(images.ImageError):
        preview.render(solid((0, 0, 0)), solid((0, 0, 0)), width=1)
    with pytest.raises(images.ImageError):
        preview.size_for(640, "nonsense")
