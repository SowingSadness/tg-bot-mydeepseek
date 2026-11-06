"""Композит-превью «A|B» для inline-фото: два кадра рядом, разделитель и подписи.

Превью должно показывать **то же**, что и шторка в бабле: обе картинки режутся
по одному окну `fit_box(img_a, aspect)` (окно считается по первой картинке и
применяется к обеим — как в `build.build`), поэтому расхождение с баблом
возможно только по масштабу, но не по участку сцены.

Модуль не импортирует `bot.config`: ширина, пропорции и подписи приходят
аргументами. Подписи рисуются best-effort: если TTF-шрифта с кириллицей нет,
композит собирается без текста (падать нельзя — это украшение inline-выдачи).
"""

from __future__ import annotations

import logging
from fractions import Fraction
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .images import ImageError, crop, fit_box

log = logging.getLogger(__name__)

#: Толщина белой линии по центру и тёмной обводки с каждой стороны.
DIVIDER_WIDTH = 6
DIVIDER_OUTLINE = 2
WHITE = (255, 255, 255)
DIVIDER_EDGE = (24, 24, 24)
#: Цвет плашки под подписью и самого текста.
LABEL_BG = (0, 0, 0, 170)
LABEL_FG = (255, 255, 255)
#: Высота текста как доля высоты кадра (и отступ плашки от края).
LABEL_RATIO = 0.075
LABEL_MARGIN_RATIO = 0.02
#: Где ищем TTF с кириллицей (Windows, Linux, macOS); первый найденный — используется.
FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def _aspect_pair(aspect) -> tuple[int, int]:
    """`'3:2'`, `'2.66:1'` или `(3, 2)` → целая пара положительных чисел.

    Та же нормализация, что в `build.build` (дробные пропорции сводятся к целым
    через `limit_denominator`), поэтому высота композита совпадает с высотой слоя.
    """
    if isinstance(aspect, str):
        left, sep, right = aspect.strip().partition(":")
        if not sep:
            raise ImageError(f"Непонятные пропорции: {aspect}.")
        parts = (left, right)
    else:
        try:
            parts = tuple(aspect)
        except TypeError as exc:
            raise ImageError(f"Непонятные пропорции: {aspect}.") from exc
        if len(parts) != 2:
            raise ImageError(f"Непонятные пропорции: {aspect}.")
    try:
        left_frac, right_frac = (Fraction(str(part)) for part in parts)
    except (ValueError, ZeroDivisionError) as exc:
        raise ImageError(f"Непонятные пропорции: {aspect}.") from exc
    if left_frac <= 0 or right_frac <= 0:
        raise ImageError("Пропорции картинки заданы неверно.")
    ratio = (left_frac / right_frac).limit_denominator(1000)
    return ratio.numerator, ratio.denominator


def size_for(width: int, aspect="3:2") -> tuple[int, int]:
    """`(ширина композита, высота)` для общей `width`: половина — на каждый кадр."""
    width = int(width)
    if width < 2:
        raise ImageError("Ширина превью должна быть хотя бы 2 пикселя.")
    aw, ah = _aspect_pair(aspect)
    half = width // 2
    return half * 2, max(1, round(half * ah / aw))


def _find_font(size: int) -> ImageFont.FreeTypeFont | None:
    """Первый доступный TTF (best-effort): без шрифта подписи просто не рисуются."""
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        try:
            if path.is_file():
                return ImageFont.truetype(str(path), size=size)
        except (OSError, ValueError):  # битый/неподдерживаемый файл — пробуем дальше
            continue
    return None


def _draw_label(
    draw: ImageDraw.ImageDraw,
    text: str,
    box_origin: tuple[int, int],
    box_size: tuple[int, int],
    *,
    align_right: bool,
) -> None:
    """Плашка с текстом в верхнем углу кадра; `textanchor="lt"` — предсказуемо."""
    font = _find_font(max(8, int(box_size[1] * LABEL_RATIO)))
    if font is None or not text:
        return

    margin = max(4, int(box_size[1] * LABEL_MARGIN_RATIO))
    padding = max(3, margin // 2)
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    except (OSError, ValueError):  # экзотический шрифт без метрик — без подписи
        return
    text_w, text_h = right - left, bottom - top
    plate_w, plate_h = text_w + padding * 2, text_h + padding * 2
    x0 = box_origin[0] + margin
    if align_right:
        x0 = box_origin[0] + box_size[0] - margin - plate_w
    y0 = box_origin[1] + margin

    draw.rectangle(
        (x0, y0, x0 + plate_w, y0 + plate_h),
        fill=LABEL_BG,
    )
    draw.text(
        (x0 + padding - left, y0 + padding - top),
        text,
        font=font,
        fill=LABEL_FG,
    )


def _draw_divider(canvas: Image.Image, center_x: int) -> None:
    """Белая линия по центру с тонкой тёмной обводкой (видна на светлом кадре)."""
    draw = ImageDraw.Draw(canvas)
    half_line = DIVIDER_WIDTH / 2.0
    half_outline = half_line + DIVIDER_OUTLINE
    draw.rectangle(
        (center_x - half_outline, 0, center_x + half_outline, canvas.height),
        fill=DIVIDER_EDGE,
    )
    draw.rectangle(
        (center_x - half_line, 0, center_x + half_line, canvas.height),
        fill=WHITE,
    )


def render(
    img_a: Image.Image,
    img_b: Image.Image,
    *,
    width: int = 2560,
    aspect="3:2",
    labels: tuple[str, str] = ("До", "После"),
    quality: int = 85,
) -> bytes:
    """Собирает композит «A|B» и возвращает JPEG-байты.

    Каждый кадр — полный кадр исходника, обрезанный по общему окну
    `fit_box(img_a, aspect)` и приведённый к `aspect` (как слои бабла).
    `width` — общая ширина композита, каждый кадр получает `width // 2`.
    """
    total_width, height = size_for(width, aspect)
    half = total_width // 2
    aw, ah = _aspect_pair(aspect)
    quality = int(quality)
    if not 1 <= quality <= 100:
        raise ImageError("Качество JPEG должно быть в диапазоне 1..100.")

    # Окно считается по первой картинке и применяется к обеим — тот же участок сцены.
    window = fit_box(img_a, (aw, ah))
    frame_a = crop(img_a, window).resize((half, height), Image.LANCZOS)
    frame_b = crop(img_b, window).resize((half, height), Image.LANCZOS)

    canvas = Image.new("RGB", (total_width, height), WHITE)
    canvas.paste(frame_a, (0, 0))
    canvas.paste(frame_b, (half, 0))

    try:
        label_a, label_b = (str(labels[0]), str(labels[1])) if labels else ("", "")
    except (TypeError, IndexError):
        label_a, label_b = "", ""
    try:  # подписи — украшение: любая беда со шрифтом не должна ломать превью
        draw = ImageDraw.Draw(canvas, "RGBA")
        _draw_label(draw, label_a, (0, 0), (half, height), align_right=False)
        _draw_label(draw, label_b, (half, 0), (half, height), align_right=True)
    except Exception as exc:  # noqa: BLE001 — best-effort, без падения
        log.debug("Подписи превью не нарисованы: %s", exc)
    _draw_divider(canvas, half)

    buffer = BytesIO()
    try:
        canvas.save(buffer, format="JPEG", quality=quality, optimize=True)
    except (OSError, ValueError) as exc:
        raise ImageError("Не удалось собрать превью сравнения.") from exc
    blob = buffer.getvalue()
    if not blob:
        raise ImageError("Не удалось собрать превью сравнения: пустой файл.")
    return blob


__all__ = ["DIVIDER_OUTLINE", "DIVIDER_WIDTH", "render", "size_for"]
