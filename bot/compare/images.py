"""Картинки для сравнения: декод, EXIF, центральный кроп 4:3, WebP и скачивание по ссылке.

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
from PIL import Image, ImageOps, UnidentifiedImageError

# Больше 40 Мп не декодируем: и память, и время, а для бабла всё равно нужно ~430 px.
MAX_PIXELS = 40_000_000
WEBP_PREFIX = "data:image/webp;base64,"
# Ширина/высота бабла: картинки всегда кропаются под 4:3.
ASPECT_W, ASPECT_H = 4, 3
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
    """Сырые WebP-байты → `data:image/webp;base64,...` для `src` в шаблоне."""
    return WEBP_PREFIX + base64.b64encode(blob).decode("ascii")


def to_webp(img: Image.Image, width: int, quality: int) -> tuple[str, bytes]:
    """Центр-кроп под 4:3, ресайз ровно в `(width, round(width*3/4))` и WebP.

    Возвращает `(data-URL, сырые байты)`: data-URL идёт в HTML, байты — в кеш.
    """
    width = int(width)
    if width < 1:
        raise ImageError("Ширина картинки должна быть положительной.")
    height = max(1, round(width * ASPECT_H / ASPECT_W))

    try:
        fitted = ImageOps.fit(img, (width, height), method=Image.LANCZOS, centering=(0.5, 0.5))
        buffer = BytesIO()
        fitted.save(buffer, format="WEBP", quality=int(quality), method=6)
    except (OSError, ValueError) as exc:
        raise ImageError("Не удалось сжать картинку в WebP.") from exc

    blob = buffer.getvalue()
    if not blob:
        raise ImageError("Не удалось сжать картинку в WebP: получился пустой файл.")
    return webp_data_url(blob), blob


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
