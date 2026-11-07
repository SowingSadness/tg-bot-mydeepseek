"""Тесты композита-превью «половинами»: размеры, половины, разделитель, подписи."""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageChops, ImageDraw

from bot.compare import images, preview


def solid(color: tuple[int, int, int], size: tuple[int, int] = (400, 200)) -> Image.Image:
    return Image.new("RGB", size, color)


def gradient(
    size: tuple[int, int] = (400, 200),
    *,
    low: int = 16,
    high: int = 238,
    reverse: bool = False,
) -> Image.Image:
    """Плавный горизонтальный градиент: JPEG на нём почти не даёт артефактов."""
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    width, height = size
    for x in range(width):
        t = x / (width - 1) if width > 1 else 0.0
        if reverse:
            t = 1.0 - t
        value = round(low + (high - low) * t)
        draw.line((x, 0, x, height - 1), fill=(value, value, value))
    return img


def decode(blob: bytes) -> Image.Image:
    return Image.open(BytesIO(blob)).convert("RGB")


def fitted_pair(
    img_a: Image.Image,
    img_b: Image.Image,
    *,
    width: int = 1200,
    aspect=(3, 2),
) -> tuple[Image.Image, Image.Image]:
    """Ожидаемые кадры: то же окно кропа (по A), что и у слоёв бабла, полный размер."""
    total_width, height = preview.size_for(width, aspect)
    aspect_pair = preview._aspect_pair(aspect)
    window = images.fit_box(img_a, aspect_pair)
    size = (total_width, height)
    return (
        images.crop(img_a, window).resize(size, Image.LANCZOS),
        images.crop(img_b, window).resize(size, Image.LANCZOS),
    )


def max_channel_diff(first: Image.Image, second: Image.Image) -> int:
    diff = ImageChops.difference(first, second)
    return max(upper for _lower, upper in diff.getextrema())


def assert_pixels_close(actual: Image.Image, expected: Image.Image, *, tol: int = 3) -> None:
    diff = ImageChops.difference(actual, expected)
    extrema = diff.getextrema()
    assert max(upper for _lower, upper in extrema) <= tol, extrema


def assert_pixel_close(actual, expected, *, tol: int = 3) -> None:
    assert all(abs(a - e) <= tol for a, e in zip(actual, expected)), (actual, expected)


def test_render_returns_jpeg_with_3_2_size():
    blob = preview.render(solid((200, 0, 0)), solid((0, 0, 200)), width=1200)

    assert blob[:2] == b"\xff\xd8"  # JPEG-сигнатура
    assert blob[-2:] == b"\xff\xd9"  # EOI: файл целый
    image = Image.open(BytesIO(blob))
    assert image.format == "JPEG"
    # Композит — одна сцена 3:2: 1200×800, а не два кадра 3:1.
    assert image.size == (1200, 800)
    assert image.size == preview.size_for(1200)
    assert image.width * 2 == image.height * 3  # ровно 3:2
    # Рабочая ширина по умолчанию: 2560×1707 (та же сцена 3:2).
    assert preview.size_for(2560) == (2560, 1707)


def test_left_half_is_left_50_of_a_and_right_half_is_right_50_of_b(monkeypatch):
    monkeypatch.setattr(preview, "FONT_CANDIDATES", ())  # без плашек — сравниваем пиксели
    img_a = gradient(low=20, high=120)
    img_b = gradient(low=140, high=240, reverse=True)

    blob = preview.render(img_a, img_b, width=1200, aspect="3:2", quality=95)
    image = decode(blob)
    fitted_a, fitted_b = fitted_pair(img_a, img_b, width=1200, aspect="3:2")
    half = image.width // 2
    # Разделитель (белая линия + обводка) рисуется поверх стыка — его не сравниваем.
    guard = preview.DIVIDER_WIDTH // 2 + preview.DIVIDER_OUTLINE + 2

    left_actual = image.crop((0, 0, half - guard, image.height))
    left_expected = fitted_a.crop((0, 0, half - guard, image.height))
    assert_pixels_close(left_actual, left_expected)

    right_actual = image.crop((half + guard, 0, image.width, image.height))
    right_expected = fitted_b.crop((half + guard, 0, image.width, image.height))
    assert_pixels_close(right_actual, right_expected)

    # Половины не перепутаны и не собраны из целых кадров.
    assert max_channel_diff(left_actual, fitted_b.crop((0, 0, half - guard, image.height))) > 20
    assert max_channel_diff(right_actual, fitted_a.crop((half + guard, 0, image.width, image.height))) > 20


def test_outer_edges_come_from_outer_edges_of_frames_not_center(monkeypatch):
    monkeypatch.setattr(preview, "FONT_CANDIDATES", ())
    img_a = gradient(low=10, high=250)
    img_b = gradient(low=30, high=230, reverse=True)

    image = decode(preview.render(img_a, img_b, width=1200, quality=95))
    fitted_a, fitted_b = fitted_pair(img_a, img_b, width=1200)

    # Левый верхний угол композита — левый верхний угол A, а не центр кадра.
    assert_pixel_close(image.getpixel((0, 0)), fitted_a.getpixel((0, 0)))
    # Правый верхний угол — правый верхний угол B.
    assert_pixel_close(
        image.getpixel((image.width - 1, 0)),
        fitted_b.getpixel((fitted_b.width - 1, 0)),
    )
    # Контроль: центр A заметно светлее его левого края, значит подмена была бы видна.
    assert fitted_a.getpixel((fitted_a.width // 2, 0))[0] - fitted_a.getpixel((0, 0))[0] > 50
    assert image.getpixel((0, 0))[0] < fitted_a.getpixel((fitted_a.width // 2, 0))[0] - 50


def test_center_has_white_divider_with_dark_outline():
    blob = preview.render(solid((230, 40, 40)), solid((40, 40, 230)), width=1200)
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

    blob = preview.render(source, solid((10, 10, 250), (400, 200)), width=1200)
    image = decode(blob)

    # Левый край композита — левый край окна A, а не исходника с зелёной полосой.
    for x in (0, 1, 2, image.width // 4):
        pixel = image.getpixel((x, image.height // 2))
        assert pixel[0] > 200 and pixel[1] < 80, (x, pixel)


def test_labels_are_drawn_in_top_corners_when_font_available():
    if preview._find_font(24) is None:
        pytest.skip("TTF с кириллицей не найден — подписи best-effort не рисуются")

    blob = preview.render(
        solid((255, 255, 255)),
        solid((255, 255, 255)),
        width=1200,
        labels=("До", "После"),
    )
    image = decode(blob)
    half = image.width // 2
    top = image.height // 4
    left = image.crop((0, 0, half // 2, top)).convert("L")
    right = image.crop((half + half // 2, 0, image.width, top)).convert("L")

    # Плашка подписи полупрозрачная и тёмная: на белом фоне появляются тёмные пиксели.
    assert any(pixel < 128 for pixel in left.get_flattened_data())
    assert any(pixel < 128 for pixel in right.get_flattened_data())


def test_render_without_any_font_still_returns_jpeg(monkeypatch):
    """Нет TTF — композит без подписей, но валидный JPEG (падений быть не должно)."""
    monkeypatch.setattr(preview, "FONT_CANDIDATES", ())

    blob = preview.render(solid((0, 120, 0)), solid((120, 0, 0)), width=320)

    assert blob[:2] == b"\xff\xd8"
    assert Image.open(BytesIO(blob)).size == (320, 213)

    for labels in (("", ""), (), None):
        blob = preview.render(solid((0, 0, 0)), solid((255, 255, 255)), width=64, labels=labels)
        assert blob[:2] == b"\xff\xd8", labels


def test_aspect_variants_and_invalid_input():
    blob = preview.render(solid((9, 9, 9)), solid((200, 200, 200)), width=640, aspect="4:3")

    assert Image.open(BytesIO(blob)).size == (640, 480)
    assert preview.size_for(1200) == (1200, 800)
    assert preview.size_for(1000) == (1000, round(1000 * 2 / 3))
    assert preview.size_for(1000, "4:3") == (1000, 750)

    with pytest.raises(images.ImageError):
        preview.render(solid((0, 0, 0)), solid((0, 0, 0)), width=1)
    with pytest.raises(images.ImageError):
        preview.size_for(640, "nonsense")
