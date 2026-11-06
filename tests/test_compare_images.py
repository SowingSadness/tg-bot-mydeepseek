"""Тесты картинок: декод/EXIF, центр-кроп 4:3, WebP и скачивание по ссылке. Без сети."""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from PIL import Image

from bot.compare import images
from bot.compare.images import ImageError, download_url, load, to_webp, webp_data_url

PUBLIC_IP = "93.184.216.34"  # бывший адрес example.com, публичный


# --------------------------------------------------------------------------- утилиты


def _png(img: Image.Image) -> bytes:
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _dot_png(width: int = 1000, height: int = 500) -> bytes:
    """Чёрная картинка с одной белой точкой ровно в центре."""
    img = Image.new("RGB", (width, height), "black")
    img.putpixel((width // 2, height // 2), (255, 255, 255))
    return _png(img)


def _decode_webp(blob: bytes) -> Image.Image:
    return Image.open(BytesIO(blob)).convert("RGB")


def _brightest(img: Image.Image) -> tuple[int, int]:
    pixels = img.load()
    width, height = img.size
    best = (-1, -1, -1)
    for y in range(height):
        for x in range(width):
            value = sum(pixels[x, y])
            if value > best[0]:
                best = (value, x, y)
    return best[1], best[2]


# --------------------------------------------------------------------------- load


def test_load_returns_rgb_and_transposes_exif():
    """EXIF-поворот применяется до кропа: 100×50 с orientation=6 → 50×100."""
    img = Image.new("RGB", (100, 50), "white")
    exif = Image.Exif()
    exif[274] = 6  # rotate 90° CW
    buffer = BytesIO()
    img.save(buffer, format="JPEG", exif=exif.tobytes())

    loaded = load(buffer.getvalue())

    assert loaded.size == (50, 100)
    assert loaded.mode == "RGB"


def test_load_drops_alpha():
    img = Image.new("RGBA", (32, 24), (255, 0, 0, 128))
    loaded = load(_png(img))
    assert loaded.mode == "RGB"
    assert loaded.size == (32, 24)


def test_load_rejects_garbage():
    with pytest.raises(ImageError) as exc:
        load(b"this is definitely not an image")
    assert "не картинка" in str(exc.value).lower() or "не изображение" in str(exc.value).lower()


def test_load_rejects_empty():
    with pytest.raises(ImageError):
        load(b"")


def test_load_rejects_too_many_pixels(monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 100)
    with pytest.raises(ImageError, match="слишком большая"):
        load(_png(Image.new("RGB", (20, 20), "white")))


# --------------------------------------------------------------------------- to_webp


def test_to_webp_center_crops_under_4_3():
    """Из 1000×500 белая точка в центре попадает в (200,150) после кропа в 400×300."""
    blob = to_webp(load(_dot_png(1000, 500)), 400, 30)[1]
    decoded = _decode_webp(blob)

    assert decoded.size == (400, 300)
    x, y = _brightest(decoded)
    assert abs(x - 200) <= 1, f"ярчайший пиксель по X: {x}"
    assert abs(y - 150) <= 1, f"ярчайший пиксель по Y: {y}"


def test_to_webp_size_is_exactly_4_3():
    img = Image.new("RGB", (900, 700), "white")
    for width in (400, 160):
        url, blob = to_webp(img, width, 30)
        decoded = _decode_webp(blob)
        assert decoded.size[0] * 3 == decoded.size[1] * 4
        assert decoded.size == (width, round(width * 3 / 4))
        assert url == webp_data_url(blob)


def test_to_webp_returns_data_url_and_webp_bytes():
    url, blob = to_webp(Image.new("RGB", (800, 600), "teal"), 200, 34)

    assert url.startswith("data:image/webp;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == blob
    assert blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"


def test_to_webp_rejects_zero_width():
    with pytest.raises(ImageError):
        to_webp(Image.new("RGB", (10, 10), "white"), 0, 30)


def test_webp_data_url_is_base64_of_blob():
    blob = b"RIFF\x00\x00\x00\x00WEBPtest"
    assert webp_data_url(blob) == "data:image/webp;base64," + base64.b64encode(blob).decode()


# --------------------------------------------------------------- download_url (фейк)


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        assert size > 0
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status: int = 200, chunks: list[bytes] | None = None, headers=None) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(list(chunks or []))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    """Поддельная aiohttp-сессия: сеть не используется вообще."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if not self._responses:
            raise AssertionError(f"лишний запрос к сети: {url}")
        return self._responses.pop(0)


def _no_dns(*args, **kwargs):  # noqa: ANN002, ANN003 - подпись не важна
    raise AssertionError("скачивание по IP не должно резолвить DNS")


async def test_download_url_rejects_private_ip_without_request(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    session = _FakeSession([])

    with pytest.raises(ImageError):
        await download_url(
            session,
            "http://127.0.0.1/a.webp",
            max_bytes=1024,
            timeout=1.0,
            allow_private=False,
        )

    assert session.calls == []


async def test_download_url_rejects_private_dns(monkeypatch):
    async def fake_resolve(host: str) -> list[str]:
        assert host == "internal.example"
        return ["10.0.0.7"]

    monkeypatch.setattr(images, "_resolve", fake_resolve)
    session = _FakeSession([])

    with pytest.raises(ImageError, match="внутренний"):
        await download_url(
            session,
            "http://internal.example/a.webp",
            max_bytes=1024,
            timeout=1.0,
            allow_private=False,
        )

    assert session.calls == []


async def test_download_url_allows_private_when_configured(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    session = _FakeSession([_FakeResponse(200, [b"RIFFdata"])])

    data = await download_url(
        session,
        "http://127.0.0.1/a.webp",
        max_bytes=1024,
        timeout=1.0,
        allow_private=True,
    )

    assert data == b"RIFFdata"
    assert session.calls[0][1]["allow_redirects"] is False


async def test_download_url_reads_200(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    webp = to_webp(Image.new("RGB", (64, 48), "red"), 32, 30)[1]
    session = _FakeSession([_FakeResponse(200, [webp[:10], webp[10:]])])

    data = await download_url(
        session,
        f"https://{PUBLIC_IP}/a.webp",
        max_bytes=1024 * 1024,
        timeout=5.0,
        allow_private=False,
    )

    assert data == webp
    assert _decode_webp(data).size == (32, 24)


async def test_download_url_stops_at_max_bytes(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    session = _FakeSession([_FakeResponse(200, [b"x" * 64, b"y" * 64])])

    with pytest.raises(ImageError, match="больше"):
        await download_url(
            session,
            f"https://{PUBLIC_IP}/a.webp",
            max_bytes=100,
            timeout=1.0,
            allow_private=False,
        )


async def test_download_url_respects_content_length(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    session = _FakeSession([_FakeResponse(200, [b"x"], headers={"Content-Length": "999999"})])

    with pytest.raises(ImageError, match="больше"):
        await download_url(
            session,
            f"https://{PUBLIC_IP}/a.webp",
            max_bytes=100,
            timeout=1.0,
            allow_private=False,
        )


async def test_download_url_follows_redirect_and_checks_each_host(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    session = _FakeSession(
        [
            _FakeResponse(302, headers={"Location": f"https://{PUBLIC_IP}/b.webp"}),
            _FakeResponse(200, [b"RIFF...."]),
        ]
    )

    data = await download_url(
        session,
        f"https://{PUBLIC_IP}/a.webp",
        max_bytes=1024,
        timeout=1.0,
        allow_private=False,
    )

    assert data == b"RIFF...."
    assert [call[0] for call in session.calls] == [
        f"https://{PUBLIC_IP}/a.webp",
        f"https://{PUBLIC_IP}/b.webp",
    ]


async def test_download_url_rejects_redirect_to_private(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    session = _FakeSession([_FakeResponse(302, headers={"Location": "http://127.0.0.1/b.webp"})])

    with pytest.raises(ImageError, match="внутренний"):
        await download_url(
            session,
            f"https://{PUBLIC_IP}/a.webp",
            max_bytes=1024,
            timeout=1.0,
            allow_private=False,
        )

    assert len(session.calls) == 1


async def test_download_url_limits_redirects(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    session = _FakeSession(
        [_FakeResponse(302, headers={"Location": "/next"}) for _ in range(5)]
    )

    with pytest.raises(ImageError, match="перенаправлен"):
        await download_url(
            session,
            f"https://{PUBLIC_IP}/a.webp",
            max_bytes=1024,
            timeout=1.0,
            allow_private=False,
        )

    assert len(session.calls) == images.MAX_REDIRECTS + 1


async def test_download_url_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    session = _FakeSession([])

    with pytest.raises(ImageError, match="http"):
        await download_url(
            session,
            "ftp://example.com/a.webp",
            max_bytes=1024,
            timeout=1.0,
            allow_private=False,
        )

    assert session.calls == []


async def test_download_url_reports_http_error(monkeypatch):
    monkeypatch.setattr(images, "_resolve", _no_dns)
    session = _FakeSession([_FakeResponse(404)])

    with pytest.raises(ImageError, match="404"):
        await download_url(
            session,
            f"https://{PUBLIC_IP}/missing.webp",
            max_bytes=1024,
            timeout=1.0,
            allow_private=False,
        )
