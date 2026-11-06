"""Картинки для сравнения: декод, EXIF, кроп под кадр 3:2 (настраивается через CMP_ASPECT), WebP и скачивание по ссылке.

Модуль не зависит от `bot.config`: все лимиты и таймауты приходят аргументами, поэтому
его удобно тестировать без сети и окружения.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import aiohttp
from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

# Больше 40 Мп не декодируем: и память, и время, а для бабла всё равно нужно ~430 px.
MAX_PIXELS = 40_000_000
WEBP_PREFIX = "data:image/webp;base64,"
JPEG_PREFIX = "data:image/jpeg;base64,"
# Пропорции бабла: 3:2 (на 11% меньше пикселей, чем 4:3, при той же ширине).
ASPECT = (3, 2)
# Совместимость: снаружи могли использовать отдельные константы.
ASPECT_W, ASPECT_H = ASPECT

#: Профили энкодера, которые перебирает сборщик.
PROFILES = ("webp-lossy", "webp-quant", "jpeg")
#: Препроцессинг после ресайза: лёгкий unsharp, чтобы превью не выглядело мылом.
SHARPEN_RADIUS = 0.8
SHARPEN_PERCENT = 60
SHARPEN_THRESHOLD = 2
#: Режим soft: гауссово размытие ДО ресайза для шумных фото (экономим байты).
SOFT_BLUR = 0.4
# Сколько редиректов проходим вручную, проверяя хост на каждом шаге.
MAX_REDIRECTS = 3
_CHUNK_SIZE = 64 * 1024
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class ImageError(Exception):
    """Ошибка с понятным пользователю русским текстом (показывается как есть)."""


def load(data: bytes) -> Image.Image:
    """Декодирует картинку: EXIF-поворот (до кропа!), RGB, отсечка по числу пикселей."""
    if not data:
        raise ImageError("Пустой файл — это не картинка.")

    try:
        with Image.open(BytesIO(data)) as source:
            # exif_transpose обязан идти до кропа: иначе центральный кроп считает
            # повёрнутую картинку «боком» и режет не то.
            img = ImageOps.exif_transpose(source)
            img = img.convert("RGB")
    except UnidentifiedImageError as exc:
        raise ImageError(
            "Это не картинка или формат не поддерживается (HEIC без плагина не читается)."
        ) from exc
    except Image.DecompressionBombError as exc:
        raise ImageError("Картинка слишком большая, чтобы её обработать.") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImageError("Не удалось открыть картинку: файл повреждён или это не изображение.") from exc

    width, height = img.size
    if width <= 0 or height <= 0:
        raise ImageError("У картинки нулевой размер.")
    if width * height > MAX_PIXELS:
        megapixels = width * height / 1_000_000
        raise ImageError(
            f"Картинка слишком большая: {width}×{height} ({megapixels:.1f} Мп), "
            f"лимит — {MAX_PIXELS // 1_000_000} Мп."
        )
    return img


def webp_data_url(blob: bytes) -> str:
    """Байты WebP/JPEG → `data:` URL для `src` в шаблоне.

    Историческое имя оставлено: `build.render_cached` не знает профиль из кеша,
    поэтому формат определяется по сигнатуре байтов (WebP — по умолчанию).
    """
    prefix = JPEG_PREFIX if blob[:2] == b"\xff\xd8" else WEBP_PREFIX
    return prefix + base64.b64encode(blob).decode("ascii")


def fit_box(
    img: Image.Image,
    aspect: tuple[int, int] = ASPECT,
    centering: tuple[float, float] = (0.5, 0.5),
) -> tuple[float, float, float, float]:
    """Окно кропа под `aspect` в ДОЛЯХ исходника: `(left, top, right, bottom)`.

    Ресайза нет: если исходник шире нужного AR — режутся бока, если уже — верх/низ.
    Окно центрируется по `centering` (0..1 по каждой оси) и всегда лежит в 0..1.
    """
    aw, ah = (int(part) for part in aspect)
    if aw <= 0 or ah <= 0:
        raise ImageError("Пропорции картинки заданы неверно.")
    width, height = img.size
    if width <= 0 or height <= 0:
        raise ImageError("У картинки нулевой размер.")

    cx = min(1.0, max(0.0, float(centering[0])))
    cy = min(1.0, max(0.0, float(centering[1])))
    source_ar = width / height
    target_ar = aw / ah

    if source_ar > target_ar:  # шире нужного — режем бока
        fraction = target_ar / source_ar
        left = cx * (1.0 - fraction)
        return (left, 0.0, left + fraction, 1.0)
    if source_ar < target_ar:  # уже нужного — режем верх/низ
        fraction = source_ar / target_ar
        top = cy * (1.0 - fraction)
        return (0.0, top, 1.0, top + fraction)
    return (0.0, 0.0, 1.0, 1.0)


def crop(img: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    """Вырезает окно, заданное долями (`fit_box`), минимум в 1 пиксель."""
    width, height = img.size
    left = min(width - 1, max(0, int(round(box[0] * width))))
    top = min(height - 1, max(0, int(round(box[1] * height))))
    right = min(width, max(left + 1, int(round(box[2] * width))))
    bottom = min(height, max(top + 1, int(round(box[3] * height))))
    return img.crop((left, top, right, bottom))


def _mean_abs_error(reference: Image.Image, decoded: Image.Image) -> float:
    """Средняя абсолютная разница по каналам, нормированная в 0..1."""
    if decoded.mode != "RGB":
        decoded = decoded.convert("RGB")
    diff = ImageChops.difference(reference, decoded)
    means = ImageStat.Stat(diff).mean
    return sum(means) / len(means) / 255.0


def _encode(img: Image.Image, quality: int, profile: str) -> bytes:
    """Кодирует готовое превью в выбранный профиль (без новых зависимостей)."""
    buffer = BytesIO()
    if profile == "webp-quant":
        colors = 128 if quality >= 30 else 64
        quantized = img.quantize(colors=colors)
        quantized.save(buffer, format="WEBP", lossless=True)
    elif profile == "jpeg":
        img.save(
            buffer,
            format="JPEG",
            quality=int(quality),
            subsampling=2,
            optimize=True,
        )
    else:  # webp-lossy (и старое имя "lossy")
        img.save(
            buffer,
            format="WEBP",
            quality=int(quality),
            method=6,
            smart_subsample=True,
        )
    return buffer.getvalue()


def to_webp(
    img: Image.Image,
    width: int,
    quality: int,
    *,
    box: tuple[float, float, float, float] | None = None,
    aspect: tuple[int, int] = ASPECT,
    profile: str = "lossy",
    soft: bool = False,
) -> tuple[str, bytes, float]:
    """Кроп по окну → ресайз в AR → препроцессинг → энкод → метрика искажения.

    `box` — доли исходника (общие для обеих картинок); `None` — `fit_box(img, aspect)`.
    Возвращает `(data-URL, сырые байты, ошибка 0..1)`: URL идёт в HTML, байты — в кеш.
    """
    width = int(width)
    if width < 1:
        raise ImageError("Ширина картинки должна быть положительной.")

    aw, ah = (int(part) for part in aspect)
    if aw <= 0 or ah <= 0:
        raise ImageError("Пропорции картинки заданы неверно.")
    height = max(1, round(width * ah / aw))

    name = "webp-lossy" if profile == "lossy" else str(profile)
    if name not in PROFILES:
        raise ImageError(f"Неизвестный профиль сжатия: {profile}.")

    window = fit_box(img, aspect) if box is None else box

    try:
        cropped = crop(img, window)
        if soft:
            cropped = cropped.filter(ImageFilter.GaussianBlur(SOFT_BLUR))
        fitted = cropped.resize((width, height), Image.LANCZOS)
        fitted = fitted.filter(
            ImageFilter.UnsharpMask(
                radius=SHARPEN_RADIUS,
                percent=SHARPEN_PERCENT,
                threshold=SHARPEN_THRESHOLD,
            )
        )
        blob = _encode(fitted, int(quality), name)
        decoded = Image.open(BytesIO(blob)).convert("RGB")
    except (OSError, ValueError) as exc:
        raise ImageError("Не удалось сжать картинку в WebP.") from exc

    if not blob:
        raise ImageError("Не удалось сжать картинку в WebP: получился пустой файл.")
    error = _mean_abs_error(fitted.convert("RGB"), decoded)
    return webp_data_url(blob), blob, error


def _is_public_ip(value: str) -> bool:
    """Публичный ли адрес: private/loopback/link-local/reserved/multicast отбиваем."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _resolve(host: str) -> list[str]:
    """Резолвит хост через event loop (отдельная функция — её подменяют тесты)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _check_host(host: str, allow_private: bool) -> None:
    """Проверяет хост до запроса и на каждом редиректе (защита от SSRF)."""
    if allow_private:
        return

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _is_public_ip(host):
            raise ImageError("Ссылка ведёт на внутренний адрес — так нельзя.")
        return

    try:
        addresses = await _resolve(host)
    except (OSError, socket.gaierror, UnicodeError) as exc:
        raise ImageError(f"Не удалось разрешить адрес «{host}».") from exc
    except asyncio.TimeoutError as exc:
        raise ImageError(f"Не удалось разрешить адрес «{host}»: таймаут.") from exc

    if not addresses:
        raise ImageError(f"Не удалось разрешить адрес «{host}».")
    # Блокируем, если хоть один адрес внутренний: иначе DNS-rebinding обойдёт проверку.
    if any(not _is_public_ip(address) for address in addresses):
        raise ImageError("Ссылка ведёт на внутренний адрес — так нельзя.")


def _check_url(url: str) -> tuple[str, str]:
    """Возвращает `(схема, хост)` и отбивает всё, кроме http/https."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ImageError("Ссылка должна начинаться с http:// или https://.")
    host = parts.hostname or ""
    if not host:
        raise ImageError("В ссылке не указан адрес сайта.")
    return scheme, host


async def download_url(
    session,
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    allow_private: bool,
) -> bytes:
    """Скачивает картинку по ссылке потоком, не более `max_bytes` байт.

    Редиректы проходятся вручную (≤ `MAX_REDIRECTS`), хост проверяется на каждом шаге.
    Любая проблема — `ImageError` с текстом для пользователя, наружу ничего не течёт.
    """
    if max_bytes <= 0:
        raise ImageError("Лимит размера файла задан неверно.")
    if timeout is not None and timeout <= 0:
        raise ImageError("Таймаут скачивания задан неверно.")

    current = url
    redirects = 0
    while True:
        _, host = _check_url(current)
        await _check_host(host, allow_private)

        chunks: list[bytes] = []
        total = 0
        try:
            async with session.get(current, timeout=timeout, allow_redirects=False) as response:
                status = response.status
                location = response.headers.get("Location") if status in _REDIRECT_CODES else None

                if status in _REDIRECT_CODES:
                    if not location:
                        raise ImageError("Сайт ответил редиректом без адреса.")
                    if redirects >= MAX_REDIRECTS:
                        raise ImageError("Слишком много перенаправлений по ссылке.")
                    redirects += 1
                    current = urljoin(current, location)
                    continue

                if status != 200:
                    raise ImageError(f"Сайт ответил {status} — картинку скачать не удалось.")

                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except (TypeError, ValueError):
                        declared_size = 0
                    if declared_size > max_bytes:
                        raise ImageError(f"Файл по ссылке больше {_mib(max_bytes)}.")

                async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageError(f"Файл по ссылке больше {_mib(max_bytes)}.")
                    chunks.append(chunk)
        except ImageError:
            raise
        except asyncio.TimeoutError as exc:
            raise ImageError("Не удалось скачать картинку: сайт не ответил вовремя.") from exc
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            raise ImageError("Не удалось скачать картинку по ссылке.") from exc

        if total == 0:
            raise ImageError("По ссылке пришёл пустой файл.")
        return b"".join(chunks)


def _mib(max_bytes: int) -> str:
    """Байты → «8 МБ» для текста ошибки (без ложной точности)."""
    if max_bytes >= 1024 * 1024:
        return f"{max_bytes / (1024 * 1024):g} МБ"
    if max_bytes >= 1024:
        return f"{max_bytes / 1024:g} КБ"
    return f"{max_bytes} Б"
