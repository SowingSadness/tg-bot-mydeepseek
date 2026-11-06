"""Оригиналы сравнения для мини-аппа: WebP (фолбэк) и AVIF (если Pillow умеет).

Рядом с БД кеша лежат полноразмерные кадры обеих сторон:
`<cache_dir>/originals/<номер>-a.webp` и `<номер>-a.avif` (то же для `b`).
Кроп — то же окно, что у слоёв бабла: `fit_box(img_a, aspect)` считается по
первой картинке и применяется к обеим, поэтому мини-апп показывает тот же
участок сцены, что и шторка в чате.

Форматы: WebP q80 пишется всегда (универсальный фолбэк), AVIF q≈60 — только
если Pillow собран с AVIF (`features.check("avif")`). Ошибка AVIF не ломает
сохранение: warning в лог, WebP уже на диске.

Модуль не импортирует `bot.config`, сеть и aiogram: путь кеша, ширина и
пропорции приходят аргументами. Имя файла строится только из номера (цифры)
и стороны (`a`/`b`), поэтому `read`/`remove` безопасны даже для значения из
недоверенного токена: снаружи путь не подставить.
"""

from __future__ import annotations

import logging
import os
from fractions import Fraction
from io import BytesIO
from pathlib import Path

from PIL import Image, features

from . import images

log = logging.getLogger(__name__)

#: Стороны сравнения: ровно `a` и `b`, никаких имён файлов снаружи.
SIDES = ("a", "b")
#: Расширения файлов на диске: WebP — обязательный, AVIF — необязательный.
EXTENSIONS = ("webp", "avif")
WEBP_MIME = "image/webp"
AVIF_MIME = "image/avif"
#: Качество WebP-фолбэка и AVIF (AVIF при том же весе заметно чище).
WEBP_QUALITY = 80
AVIF_QUALITY = 60
#: Ширина оригинала по умолчанию (`CMP_ORIGINALS_WIDTH` в конфиге).
DEFAULT_WIDTH = 1600
#: Каталог оригиналов внутри кеша сравнений.
DIRECTORY = "originals"


def _directory(cache_dir) -> Path:
    """`<cache_dir>/originals` — единственное место, где лежат файлы."""
    return Path(cache_dir) / DIRECTORY


def _file(cache_dir, cmp_id: str, side: str, extension: str) -> Path:
    """Путь к файлу стороны: собирается только из проверенных номера/стороны."""
    return _directory(cache_dir) / f"{cmp_id}-{side}.{extension}"


def _key(cmp_id) -> str | None:
    """Номер сравнения как безопасное имя файла: только десятичные цифры."""
    text = str(cmp_id)
    return text if text.isascii() and text.isdigit() else None


def _aspect_pair(aspect) -> tuple[int, int]:
    """`'3:2'`, `'2.66:1'` или `(3, 2)` → целая пара положительных чисел.

    Та же нормализация, что в `build.build`/`preview.render` (дробные пропорции
    сводятся к целым через `limit_denominator`), поэтому окно кропа совпадает
    с окном слоёв бабла.
    """
    if isinstance(aspect, str):
        left, sep, right = aspect.strip().partition(":")
        if not sep:
            raise ValueError(f"Непонятные пропорции: {aspect}.")
        parts = (left, right)
    else:
        try:
            parts = tuple(aspect)
        except TypeError as exc:
            raise ValueError(f"Непонятные пропорции: {aspect}.") from exc
        if len(parts) != 2:
            raise ValueError(f"Непонятные пропорции: {aspect}.")
    try:
        left_frac, right_frac = (Fraction(str(part)) for part in parts)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Непонятные пропорции: {aspect}.") from exc
    if left_frac <= 0 or right_frac <= 0:
        raise ValueError("Пропорции картинки заданы неверно.")
    ratio = (left_frac / right_frac).limit_denominator(1000)
    return ratio.numerator, ratio.denominator


def _fit(
    img: Image.Image,
    window: tuple[float, float, float, float],
    aspect: tuple[int, int],
    width: int,
) -> Image.Image:
    """Кроп общим окном `window` → ресайз до ширины не больше `width`.

    Окно считается по `img_a` и передаётся обеим сторонам (так же, как в
    `build.build`), поэтому участок сцены одинаковый. Вверх не растягиваем:
    если после кропа картинка уже `width`, остаётся её настоящая ширина.
    """
    aw, ah = aspect
    cropped = images.crop(img, window)
    target_width = min(int(width), cropped.width)
    target_height = max(1, round(target_width * ah / aw))
    if cropped.size == (target_width, target_height):
        return cropped
    return cropped.resize((target_width, target_height), Image.LANCZOS)


def _encode(img: Image.Image, extension: str, quality: int) -> bytes:
    """Кодирует готовый кадр в WebP или AVIF; пустой результат — ошибка."""
    buffer = BytesIO()
    if extension == "avif":
        img.save(buffer, format="AVIF", quality=int(quality))
    else:
        img.save(buffer, format="WEBP", quality=int(quality), method=6)
    blob = buffer.getvalue()
    if not blob:
        raise OSError(f"Кодировщик {extension} вернул пустой файл")
    return blob


def _write(path: Path, blob: bytes) -> None:
    """Атомарная запись: временный файл рядом и `os.replace` (читатель не увидит половину)."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save(
    cache_dir,
    cmp_id,
    img_a: Image.Image,
    img_b: Image.Image,
    *,
    width: int = DEFAULT_WIDTH,
    aspect="3:2",
) -> dict[str, str]:
    """Сохраняет оригиналы обеих сторон: WebP q80 всегда, AVIF q60 при поддержке.

    Кроп и ресайз — по общему окну `fit_box(img_a, aspect)`, ширина не больше
    `width`. AVIF пишется вторым: его ошибка — warning, WebP остаётся рабочим.
    Возвращает словарь `{"a.webp": путь, "a.avif": путь, ...}` (для логов/тестов);
    отсутствующий ключ означает, что файла нет.
    """
    key = _key(cmp_id)
    if key is None:
        raise ValueError("Номер сравнения должен состоять из цифр")
    try:
        width = int(width)
    except (TypeError, ValueError) as exc:
        raise ValueError("Ширина оригинала задана неверно") from exc
    if width < 1:
        raise ValueError("Ширина оригинала должна быть положительной")
    pair = _aspect_pair(aspect)

    _directory(cache_dir).mkdir(parents=True, exist_ok=True)
    # Одно окно кропа на обе стороны: считается по `img_a`, как у слоёв бабла.
    window = images.fit_box(img_a, pair)
    with_avif = bool(features.check("avif"))
    saved: dict[str, str] = {}

    for side, img in (("a", img_a), ("b", img_b)):
        fitted = _fit(img, window, pair, width)
        webp_path = _file(cache_dir, key, side, "webp")
        _write(webp_path, _encode(fitted, "webp", WEBP_QUALITY))
        saved[f"{side}.webp"] = str(webp_path)
        if not with_avif:
            continue

        avif_path = _file(cache_dir, key, side, "avif")
        try:
            _write(avif_path, _encode(fitted, "avif", AVIF_QUALITY))
        except Exception as exc:  # noqa: BLE001 — AVIF необязателен, WebP уже на диске
            log.warning("Не удалось закодировать AVIF #%s (%s): %s", key, side, exc)
            avif_path.unlink(missing_ok=True)
            continue
        saved[f"{side}.avif"] = str(avif_path)

    return saved


def read(
    cache_dir,
    cmp_id,
    side: str,
    *,
    accept: str = "webp",
) -> tuple[bytes, str] | None:
    """Байты стороны и фактический формат: `("avif"|"webp")` или `None`.

    `accept` — заголовок `Accept` (например `image/avif,image/webp,*/*`):
    если в нём есть `image/avif` и файл на диске, отдаётся AVIF, иначе WebP.
    Неизвестная сторона, нецифровой номер и отсутствие файлов — `None`
    (без исключений: значения приходят из недоверенного токена).
    """
    key = _key(cmp_id)
    if key is None or side not in SIDES:
        return None

    if "image/avif" in str(accept or "").lower():
        blob = _read_file(_file(cache_dir, key, side, "avif"))
        if blob:
            return blob, "avif"

    blob = _read_file(_file(cache_dir, key, side, "webp"))
    if blob:
        return blob, "webp"
    return None


def _read_file(path: Path) -> bytes | None:
    """Читает файл; нет файла или ошибка чтения — `None` (и warning на ошибку)."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Не удалось прочитать оригинал %s: %s", path.name, exc)
        return None


def remove(cache_dir, cmp_id) -> None:
    """Удаляет все файлы номера (WebP и AVIF обеих сторон); повторный вызов безопасен."""
    key = _key(cmp_id)
    if key is None:
        return
    for side in SIDES:
        for extension in EXTENSIONS:
            try:
                _file(cache_dir, key, side, extension).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Не удалось удалить %s-%s.%s: %s", key, side, extension, exc)


def remove_many(cache_dir, ids) -> None:
    """Удаляет оригиналы для набора номеров (например, после `store.purge`)."""
    for cmp_id in ids or ():
        remove(cache_dir, cmp_id)


__all__ = [
    "AVIF_MIME",
    "AVIF_QUALITY",
    "DEFAULT_WIDTH",
    "SIDES",
    "WEBP_MIME",
    "WEBP_QUALITY",
    "read",
    "remove",
    "remove_many",
    "save",
]
