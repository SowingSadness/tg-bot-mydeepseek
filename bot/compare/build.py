"""Сборка HTML-бабла сравнения: подстановка в шаблон и подгонка под лимиты Telegram.

Сервер принимает виджет, только если HTML ≤ 32 768 байт и `zlib(level 9)` ≤ 4096 байт.
Поэтому картинки пережимаются: ширина общая для обеих (иначе шторка «поедет»),
а профиль и качество подбираются независимо по метрике искажения.

Модуль намеренно не импортирует `bot.config`: лимиты и подписи приходят аргументами,
а CLI (`python -m bot.compare.build`) работает без окружения бота.

    python -m bot.compare.build --a before.jpg --b after.jpg [--out f.html] [--report]
    python -m bot.compare.build --demo [--out f.html]
    python -m bot.compare.build --check f.html
"""

from __future__ import annotations

import argparse
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .images import ASPECT, PROFILES, ImageError, fit_box, load, to_webp, webp_data_url

RAW_LIMIT = 32768
Z_LIMIT = 4096
_TPL_NAME = "template.html"
_template_cache: str | None = None
#: Потолок числа кодирований на один вызов: бот должен отвечать за ~2–3 с.
MAX_ENCODES = 120

_PROFILE_ORDER = {"webp-lossy": 0, "webp-quant": 1, "jpeg": 2}


class WidgetTooLarge(Exception):
    """Даже самый маленький вариант не влезает в лимиты виджета."""


@dataclass(frozen=True)
class Attempt:
    """Одна проверенная пара кандидатов (для `--report` и выбора победителя)."""

    width: int
    profile_a: str
    quality_a: int
    profile_b: str
    quality_b: int
    bytes_a: int
    bytes_b: int
    picture_bytes: int
    html_bytes: int
    zlib_bytes: int
    err_a: float
    err_b: float
    fits: bool

    @property
    def worst_err(self) -> float:
        """Худшая из двух ошибок: её и минимизируем."""
        return max(self.err_a, self.err_b)


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
    aspect: str = "3:2"
    quality_a: int = 0
    quality_b: int = 0
    err_a: float = 0.0
    err_b: float = 0.0
    profile_a: str = "webp-lossy"
    profile_b: str = "webp-lossy"


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


def _aspect_css(aspect: str) -> str:
    """`'3:2'` → `'3 / 2'` для `aspect-ratio` в CSS."""
    return str(aspect).strip().replace(":", " / ")


def render(
    a_url: str,
    b_url: str,
    *,
    labels: tuple[str, str],
    title: str,
    hint: str,
    aspect: str = "3:2",
) -> str:
    """Подставляет значения в шаблон.

    Только `str.replace`: в CSS шаблона есть фигурные скобки, `str.format` его сломает.
    """
    label_a, label_b = labels
    replacements = (
        ("{{TITLE}}", _escape(title)),
        ("{{AR}}", _aspect_css(aspect)),
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
    aspect: str = "3:2",
) -> str:
    """Пересобирает HTML из закешированных картинок — быстро, без сети и кодирования."""
    return render(
        webp_data_url(image_a),
        webp_data_url(image_b),
        labels=labels,
        title=title,
        hint=hint,
        aspect=aspect,
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
    """Качества попыток: от стартового вниз шагом 4, последняя ровно `min_quality`."""
    start_quality = max(int(start_quality), int(min_quality), 1)
    min_quality = max(int(min_quality), 1)
    qualities = list(range(start_quality, min_quality - 1, -4)) or [min_quality]
    if qualities[-1] != min_quality:
        qualities.append(min_quality)
    return qualities


def _aspect_pair(aspect) -> tuple[int, int]:
    """Принимает `(3, 2)` или `'3:2'` и возвращает пару положительных чисел."""
    if isinstance(aspect, str):
        left, _, right = aspect.partition(":")
        try:
            pair = (int(left), int(right))
        except ValueError as exc:
            raise ImageError(f"Непонятные пропорции: {aspect}.") from exc
    else:
        pair = (int(aspect[0]), int(aspect[1]))
    if pair[0] <= 0 or pair[1] <= 0:
        raise ImageError("Пропорции картинки заданы неверно.")
    return pair


def _candidates(qualities: list[int], *, include_soft: bool) -> list[tuple[str, int, bool]]:
    """Кандидаты на картинку: все профили × качества, опционально soft-вариант lossy.

    Порядок важен: при равной ошибке выбирается первый, поэтому сначала идут
    более качественные и более предсказуемые варианты.
    """
    result: list[tuple[str, int, bool]] = []
    for quality in qualities:
        result.append(("webp-lossy", quality, False))
        if include_soft:
            result.append(("webp-lossy", quality, True))
        result.append(("webp-quant", quality, False))
        result.append(("jpeg", quality, False))
    return result


def _encode_all(
    img: Image.Image,
    width: int,
    box: tuple[float, float, float, float],
    aspect: tuple[int, int],
    candidates: list[tuple[str, int, bool]],
    cache: dict[tuple[str, int, bool], tuple[str, bytes, float]] | None = None,
) -> tuple[dict[tuple[str, int, bool], tuple[str, bytes, float]], int]:
    """Кодирует недостающих кандидатов для одной картинки ровно по одному разу."""
    result = {} if cache is None else cache
    encoded = 0
    for profile, quality, soft in candidates:
        key = (profile, quality, soft)
        if key in result:
            continue
        result[key] = to_webp(
            img,
            width,
            quality,
            box=box,
            aspect=aspect,
            profile=profile,
            soft=soft,
        )
        encoded += 1
    return result, encoded


def _pairs(
    cache_a: dict[tuple[str, int, bool], tuple[str, bytes, float]],
    cache_b: dict[tuple[str, int, bool], tuple[str, bytes, float]],
    *,
    width: int,
    budget: int,
    labels: tuple[str, str],
    title: str,
    hint: str,
    aspect: str,
) -> tuple[tuple[Attempt, bytes, bytes, str] | None, list[Attempt]]:
    """Перебирает пары кандидатов: лучшая влезающая по `max(err)` и все строки."""
    best: tuple[Attempt, bytes, bytes, str] | None = None
    rows: list[Attempt] = []
    for (profile_a, quality_a, _), (url_a, blob_a, err_a) in cache_a.items():
        for (profile_b, quality_b, _), (url_b, blob_b, err_b) in cache_b.items():
            html = render(
                url_a,
                url_b,
                labels=labels,
                title=title,
                hint=hint,
                aspect=aspect,
            )
            raw = len(html.encode("utf-8"))
            z = zsize(html)
            attempt = Attempt(
                width=width,
                profile_a=profile_a,
                quality_a=quality_a,
                profile_b=profile_b,
                quality_b=quality_b,
                bytes_a=len(blob_a),
                bytes_b=len(blob_b),
                picture_bytes=len(blob_a) + len(blob_b),
                html_bytes=raw,
                zlib_bytes=z,
                err_a=err_a,
                err_b=err_b,
                fits=raw <= RAW_LIMIT and z <= budget,
            )
            if attempt.fits and (
                best is None
                or attempt.worst_err < best[0].worst_err - 1e-9
                or (
                    abs(attempt.worst_err - best[0].worst_err) <= 1e-9
                    and _PROFILE_ORDER[attempt.profile_a] < _PROFILE_ORDER[best[0].profile_a]
                )
            ):
                best = (attempt, blob_a, blob_b, html)
            rows.append(attempt)
    return best, rows


def _smaller(current: Attempt | None, attempt: Attempt) -> Attempt:
    """Что считать «самым маленьким» для диагностики: уже и дешевле."""
    if current is None:
        return attempt
    key = (attempt.width, max(attempt.quality_a, attempt.quality_b), attempt.picture_bytes)
    best_key = (
        current.width,
        max(current.quality_a, current.quality_b),
        current.picture_bytes,
    )
    return attempt if key < best_key else current


def _too_large(smallest: Attempt, margin: int) -> WidgetTooLarge:
    """Подробная диагностика: какой самый маленький вариант и насколько не влез."""
    budget = Z_LIMIT - int(margin)
    over = max(
        smallest.html_bytes - RAW_LIMIT,
        smallest.zlib_bytes - budget,
        0,
    )
    return WidgetTooLarge(
        "Картинки слишком пёстрые, чтобы уместиться в бабл: "
        f"даже {smallest.width}px q{smallest.quality_a}/{smallest.quality_b} "
        f"({smallest.profile_a}/{smallest.profile_b}) не влезает "
        f"({smallest.html_bytes} B HTML / {smallest.zlib_bytes} B zlib, "
        f"перебор {over} B при лимитах {RAW_LIMIT} B HTML и {budget} B zlib)."
    )


def build(
    img_a,
    img_b,
    *,
    start_width: int = 430,
    min_width: int = 160,
    start_quality: int = 34,
    min_quality: int = 14,
    margin: int = 64,
    labels: tuple[str, str] = ("До", "После"),
    title: str = "Сравнение",
    hint: str = "Проведите ↔",
    aspect: tuple[int, int] | str = ASPECT,
    report: list[Attempt] | None = None,
) -> Built:
    """Подбирает ширину, профили и качество так, чтобы HTML и `zlib` влезли в лимиты.

    Окно кропа считается ОДИН раз по первой картинке и применяется к обеим, иначе
    при разных пропорциях источников сравнивались бы разные участки сцены.
    Ширина одна для обеих (шторка не поедет), профиль и качество — независимо.

    Поиск двухфазный: на каждой ширине сначала кодируются самые дешёвые кандидаты
    (все профили на `min_quality`) — если даже они не влезают, ширина пропускается;
    на первой влезающей ширине кодируются все качества/профили и выбирается пара
    с лучшим `max(err_a, err_b)`. Так число кодирований ограничено `MAX_ENCODES`,
    а результат — не «первое, что влезло», а лучший по метрике.

    `report`, если передан, наполняется проверенными парами (для `--report`).
    """
    aw, ah = _aspect_pair(aspect)
    aspect_tuple = (aw, ah)
    aspect_str = f"{aw}:{ah}"
    budget = Z_LIMIT - int(margin)
    box = fit_box(img_a, aspect_tuple)
    qualities = _qualities(start_quality, min_quality)
    widths = _widths(start_width, min_width)

    encodings = 0
    smallest: Attempt | None = None

    for index, width in enumerate(widths):
        remaining = MAX_ENCODES - encodings
        if remaining < 6:  # на дешёвый шаг (3 профиля × 2 картинки) уже не хватает
            break

        # Фаза 1: самые дешёвые кандидаты всех профилей на min_quality.
        scout = [(profile, qualities[-1], False) for profile in PROFILES]
        cache_a, spent_a = _encode_all(img_a, width, box, aspect_tuple, scout)
        cache_b, spent_b = _encode_all(img_b, width, box, aspect_tuple, scout)
        encodings += spent_a + spent_b

        fitting, rows = _pairs(
            cache_a,
            cache_b,
            width=width,
            budget=budget,
            labels=labels,
            title=title,
            hint=hint,
            aspect=aspect_str,
        )
        if report is not None:
            report.extend(rows)
        for row in rows:
            smallest = _smaller(smallest, row)
        if fitting is None:
            continue

        # Фаза 2: на первой влезающей ширине перебираем все качества и профили.
        full_remaining = MAX_ENCODES - encodings
        best, blob_a, blob_b, html = fitting
        if full_remaining >= 2:
            candidates = _candidates(qualities, include_soft=index > 0)
            limit = max(3, full_remaining // 2)
            if len(candidates) > limit:
                candidates = candidates[:limit]
            cache_a, spent_a = _encode_all(
                img_a, width, box, aspect_tuple, candidates, cache_a
            )
            cache_b, spent_b = _encode_all(
                img_b, width, box, aspect_tuple, candidates, cache_b
            )
            encodings += spent_a + spent_b

            found, rows = _pairs(
                cache_a,
                cache_b,
                width=width,
                budget=budget,
                labels=labels,
                title=title,
                hint=hint,
                aspect=aspect_str,
            )
            if report is not None:
                report.extend(rows)
            for row in rows:
                smallest = _smaller(smallest, row)
            if found is not None:
                best, blob_a, blob_b, html = found

        return Built(
            html=html,
            width=best.width,
            quality=max(best.quality_a, best.quality_b),
            zlib_bytes=best.zlib_bytes,
            html_bytes=best.html_bytes,
            picture_bytes=best.picture_bytes,
            image_a=blob_a,
            image_b=blob_b,
            aspect=aspect_str,
            quality_a=best.quality_a,
            quality_b=best.quality_b,
            err_a=best.err_a,
            err_b=best.err_b,
            profile_a=best.profile_a,
            profile_b=best.profile_b,
        )

    if smallest is None:  # недостижимо: перебор всегда даёт хотя бы одну попытку
        raise WidgetTooLarge("Не удалось собрать виджет: перебор вариантов пуст.")
    raise _too_large(smallest, margin)


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
        f"({built.quality_a}/{built.quality_b}) "
        f"profiles={built.profile_a}/{built.profile_b} aspect={built.aspect} "
        f"zlib={built.zlib_bytes} raw={built.html_bytes} "
        f"pictures={built.picture_bytes} "
        f"err_a={built.err_a:.4f} err_b={built.err_b:.4f}"
    )


def _best_by_width(attempts: list[Attempt]) -> list[Attempt]:
    """Лучшая строка на каждую ширину: влезающая по метрике, иначе лучшая вообще."""
    grouped: dict[int, list[Attempt]] = {}
    for attempt in attempts:
        grouped.setdefault(attempt.width, []).append(attempt)

    rows: list[Attempt] = []
    for width in sorted(grouped, reverse=True):
        variants = grouped[width]
        fitting = [item for item in variants if item.fits]
        if fitting:
            rows.append(min(fitting, key=lambda item: item.worst_err))
        else:
            # Не влезло: показываем самый дешёвый вариант — видно, сколько не хватило.
            rows.append(min(variants, key=lambda item: item.zlib_bytes))
    return rows


def _report_table(attempts: list[Attempt], built: Built) -> None:
    """Печатает таблицу кандидатов по ширинам и итоговую выбранную строку."""
    print(
        f"{'width':>5}  {'A':<16} {'B':<16} {'B_a':>6} {'B_b':>6} "
        f"{'raw':>6} {'zlib':>5} {'err_a':>6} {'err_b':>6}  fit"
    )
    winner: Attempt | None = None
    for attempt in _best_by_width(attempts):
        if (
            attempt.width == built.width
            and attempt.quality_a == built.quality_a
            and attempt.quality_b == built.quality_b
            and attempt.zlib_bytes == built.zlib_bytes
        ):
            winner = attempt
        print(
            f"{attempt.width:>5}  "
            f"{attempt.profile_a + ' q' + str(attempt.quality_a):<16} "
            f"{attempt.profile_b + ' q' + str(attempt.quality_b):<16} "
            f"{attempt.bytes_a:>6} {attempt.bytes_b:>6} "
            f"{attempt.html_bytes:>6} {attempt.zlib_bytes:>5} "
            f"{attempt.err_a:>6.4f} {attempt.err_b:>6.4f}  "
            f"{'+' if attempt.fits else '-'}"
        )
    if winner is None:
        winner = min(
            (item for item in attempts if item.fits),
            key=lambda item: item.worst_err,
            default=None,
        )
    if winner is not None:
        print(
            f"ВЫБРАН: width={winner.width} "
            f"A={winner.profile_a} q{winner.quality_a} "
            f"B={winner.profile_b} q{winner.quality_b} "
            f"zlib={winner.zlib_bytes} raw={winner.html_bytes} "
            f"err_a={winner.err_a:.4f} err_b={winner.err_b:.4f}"
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
    parser.add_argument(
        "--report",
        action="store_true",
        help="таблица кандидатов по ширинам и итоговый выбранный вариант",
    )
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

    attempts: list[Attempt] = []
    started = time.perf_counter()
    try:
        built = build(pair[0], pair[1], report=attempts if args.report else None)
    except WidgetTooLarge as exc:
        print(str(exc), file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - started

    path = _write(args.out, built.html) if args.out else None
    if args.report:
        _report_table(attempts, built)
        print(f"время сборки: {elapsed:.2f} с")
    _report(built, str(path) if path else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
