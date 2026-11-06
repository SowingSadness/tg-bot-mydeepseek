"""Сборка HTML-бабла сравнения: подстановка в шаблон и подгонка под лимиты Telegram.

Сервер принимает виджет, только если HTML ≤ 32 768 байт и `zlib(level 9)` ≤ 4096 байт.
Поэтому картинки пережимаются: сначала уменьшается качество WebP, потом ширина.

Модуль намеренно не импортирует `bot.config`: лимиты и подписи приходят аргументами,
а CLI (`python -m bot.compare.build`) работает без окружения бота.

    python -m bot.compare.build --a before.jpg --b after.jpg [--out f.html]
    python -m bot.compare.build --demo [--out f.html]
    python -m bot.compare.build --check f.html
"""

from __future__ import annotations

import argparse
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .images import ImageError, load, to_webp, webp_data_url

RAW_LIMIT = 32768
Z_LIMIT = 4096
_ASPECT = (4, 3)
_TPL_NAME = "template.html"
_template_cache: str | None = None


class WidgetTooLarge(Exception):
    """Даже самый маленький вариант не влезает в лимиты виджета."""


@dataclass(frozen=True)
class Built:
    """Результат подгонки: готовый HTML и то, из чего он собран (для кеша)."""

    html: str
    width: int
    quality: int
    zlib_bytes: int
    html_bytes: int
    picture_bytes: int
    image_a: bytes
    image_b: bytes


def _template() -> str:
    """Шаблон бабла читается один раз и кэшируется в модуле."""
    global _template_cache
    if _template_cache is None:
        _template_cache = Path(__file__).with_name(_TPL_NAME).read_text(encoding="utf-8")
    return _template_cache


def _escape(value: str) -> str:
    """Минимальное HTML-экранирование подписей (они попадают в разметку)."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render(a_url: str, b_url: str, *, labels: tuple[str, str], title: str, hint: str) -> str:
    """Подставляет значения в шаблон.

    Только `str.replace`: в CSS шаблона есть фигурные скобки, `str.format` его сломает.
    """
    label_a, label_b = labels
    replacements = (
        ("{{TITLE}}", _escape(title)),
        ("{{AR}}", f"{_ASPECT[0]} / {_ASPECT[1]}"),
        ("{{LABEL_A}}", _escape(label_a)),
        ("{{LABEL_B}}", _escape(label_b)),
        ("{{HINT}}", _escape(hint)),
        ("{{A}}", a_url),
        ("{{B}}", b_url),
    )
    html = _template()
    for placeholder, value in replacements:
        html = html.replace(placeholder, value)
    return html


def render_cached(
    *,
    image_a: bytes,
    image_b: bytes,
    labels: tuple[str, str],
    title: str,
    hint: str,
) -> str:
    """Пересобирает HTML из закешированных WebP — быстро, без сети и без кодирования."""
    return render(
        webp_data_url(image_a),
        webp_data_url(image_b),
        labels=labels,
        title=title,
        hint=hint,
    )


def zsize(html: str) -> int:
    """Размер `zlib.compress(html, 9)` — столько увидит сервер Telegram."""
    return len(zlib.compress(html.encode("utf-8"), 9))


def _widths(start_width: int, min_width: int) -> list[int]:
    """Ширины попыток: от стартовой к меньшим (×0.9), последняя ровно `min_width`."""
    start_width = max(int(start_width), int(min_width))
    min_width = int(min_width)
    widths: list[int] = []
    width = start_width
    while width > min_width:
        widths.append(width)
        width = int(width * 0.9)
        if width <= 0:  # страховка от бесконечного цикла на странных аргументах
            break
    widths.append(min_width)
    return widths


def _qualities(start_quality: int, min_quality: int) -> list[int]:
    """Качества WebP попыток: от стартового вниз шагом 4, последняя ровно `min_quality`."""
    start_quality = max(int(start_quality), int(min_quality), 1)
    min_quality = max(int(min_quality), 1)
    qualities = list(range(start_quality, min_quality - 1, -4)) or [min_quality]
    if qualities[-1] != min_quality:
        qualities.append(min_quality)
    return qualities


def build(
    img_a,
    img_b,
    *,
    start_width: int = 430,
    min_width: int = 160,
    start_quality: int = 34,
    min_quality: int = 16,
    margin: int = 64,
    labels: tuple[str, str] = ("До", "После"),
    title: str = "Сравнение",
    hint: str = "Проведите ↔",
) -> Built:
    """Подбирает ширину и качество так, чтобы HTML и `zlib` влезли в лимиты сервера.

    Порядок перебора: от широкой картинки к узкой (×0.9, последняя `min_width`);
    для каждой ширины — от `start_quality` вниз шагом 4 до `min_quality` (включительно).
    `margin` — запас под 4096 байт (расхождение реализаций zlib и служебные байты).
    """
    budget = Z_LIMIT - int(margin)
    smallest: Built | None = None

    for width in _widths(start_width, min_width):
        for quality in _qualities(start_quality, min_quality):
            url_a, blob_a = to_webp(img_a, width, quality)
            url_b, blob_b = to_webp(img_b, width, quality)
            html = render(url_a, url_b, labels=labels, title=title, hint=hint)
            raw = len(html.encode("utf-8"))
            z = zsize(html)
            attempt = Built(
                html=html,
                width=width,
                quality=quality,
                zlib_bytes=z,
                html_bytes=raw,
                picture_bytes=len(blob_a) + len(blob_b),
                image_a=blob_a,
                image_b=blob_b,
            )
            if raw <= RAW_LIMIT and z <= budget:
                return attempt
            smallest = attempt

    if smallest is None:  # недостижимо: перебор всегда даёт хотя бы одну попытку
        raise WidgetTooLarge("Не удалось собрать виджет: перебор вариантов пуст.")

    over = max(smallest.html_bytes - RAW_LIMIT, smallest.zlib_bytes - budget, 0)
    raise WidgetTooLarge(
        "Картинки слишком пёстрые, чтобы уместиться в бабл: "
        f"даже {smallest.width}px q{smallest.quality} не влезает "
        f"({smallest.html_bytes} B HTML / {smallest.zlib_bytes} B zlib, "
        f"перебор {over} B при лимитах {RAW_LIMIT} B HTML и {budget} B zlib)."
    )


def demo_pair(size: tuple[int, int] = (1200, 900)) -> tuple[Image.Image, Image.Image]:
    """Две разные плавные картинки для `--demo`: локально, без сети и без файлов."""
    width, height = size

    def make(black: str, white: str, accent: str, shift: float) -> Image.Image:
        ramp = Image.linear_gradient("L").resize((width, height), Image.BICUBIC)
        base = ImageOps.colorize(ramp, black=black, white=white)
        mask = Image.new("L", (width, height), 0)
        radius = min(width, height) * 0.35
        cx = width * shift
        cy = height * 0.55
        ImageDraw.Draw(mask).ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius), fill=255
        )
        mask = mask.filter(ImageFilter.GaussianBlur(min(width, height) * 0.06))
        return Image.composite(Image.new("RGB", (width, height), accent), base, mask)

    return (
        make("#0b1e3d", "#f6c56b", "#ff5c8a", 0.35),
        make("#123a2a", "#8fe3c4", "#3d6bff", 0.65),
    )


def _write(path: str, html: str) -> Path:
    target = Path(path)
    if target.parent and str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target


def _report(built: Built, path: str | None = None) -> None:
    where = f"{path}: " if path else ""
    print(
        f"{where}width={built.width} quality={built.quality} "
        f"zlib={built.zlib_bytes} raw={built.html_bytes} "
        f"pictures={built.picture_bytes}"
    )


def _check(path: str) -> int:
    html = Path(path).read_text(encoding="utf-8")
    raw = len(html.encode("utf-8"))
    z = zsize(html)
    ok = raw <= RAW_LIMIT and z <= Z_LIMIT
    print(
        f"{path}: raw={raw} (limit {RAW_LIMIT}) zlib={z} (limit {Z_LIMIT}) "
        f"{'ok' if ok else 'TOO_LARGE'}"
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bot.compare.build",
        description="Собирает HTML-бабл сравнения двух картинок и проверяет лимиты Telegram.",
    )
    parser.add_argument("--a", help="первая картинка (нижний слой)")
    parser.add_argument("--b", help="вторая картинка (верхний слой)")
    parser.add_argument("--out", help="куда записать HTML")
    parser.add_argument("--demo", action="store_true", help="синтетическая пара, без сети")
    parser.add_argument("--check", metavar="F.HTML", help="проверить готовый HTML на лимиты")
    args = parser.parse_args(argv)

    if args.check:
        return _check(args.check)

    if args.demo:
        pair = demo_pair()
    elif args.a and args.b:
        try:
            pair = (load(Path(args.a).read_bytes()), load(Path(args.b).read_bytes()))
        except OSError as exc:
            print(f"Не удалось прочитать файл: {exc}", file=sys.stderr)
            return 2
        except ImageError as exc:
            print(f"Картинка не подошла: {exc}", file=sys.stderr)
            return 2
    else:
        parser.print_usage(sys.stderr)
        print("нужно --a и --b, либо --demo, либо --check F.HTML", file=sys.stderr)
        return 2

    try:
        built = build(pair[0], pair[1])
    except WidgetTooLarge as exc:
        print(str(exc), file=sys.stderr)
        return 1

    path = _write(args.out, built.html) if args.out else None
    _report(built, str(path) if path else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
