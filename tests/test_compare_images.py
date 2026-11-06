"""Тесты картинок: декод/EXIF, общее окно кропа 3:2, WebP/JPEG и скачивание. Без сети."""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from bot.compare import images
from bot.compare.images import (
    ImageError,
    crop,
    download_url,
    fit_box,
    load,
    to_webp,
    webp_data_url,
)

PUBLIC_IP = "93.184.216.34"  # бывший адрес example.com, публичный


# --------------------------------------------------------------------------- утилиты


def _png(img: Image.Image) -> bytes:
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _dot_png(width: int = 1000, height: int = 500, *, x: int | None = None, y: int | None = None) -> bytes:
    """Чёрная картинка с белым квадратом (по умолчанию ровно в центре)."""
    img = Image.new("RGB", (width, height), "black")
    cx = width // 2 if x is None else x
    cy = height // 2 if y is None else y
    half = max(2, min(width, height) // 40)
    ImageDraw.Draw(img).rectangle((cx - half, cy - half, cx + half, cy + half), fill="white")
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


def _bright_center(img: Image.Image, threshold: int = 200) -> tuple[float, float]:
    """Центр масс ярких пикселей: у залитого квадрата `_brightest` берёт его угол."""
    pixels = img.load()
    width, height = img.size
    total = 0
    sx = sy = 0
    for y in range(height):
        for x in range(width):
            brightness = sum(pixels[x, y]) / 3
            if brightness >= threshold:
                weight = int(brightness)
                total += weight
                sx += x * weight
                sy += y * weight
    assert total, "в картинке нет ярких пикселей"
    return sx / total, sy / total


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


# --------------------------------------------------------------------------- fit_box / crop


def test_fit_box_crops_sides_of_wide_source():
    box = fit_box(Image.new("RGB", (1000, 500), "white"), (3, 2))

    assert box == pytest.approx((0.125, 0.0, 0.875, 1.0))


def test_fit_box_crops_top_bottom_of_tall_source():
    box = fit_box(Image.new("RGB", (500, 500), "white"), (3, 2))

    assert box == pytest.approx((0.0, 1 / 6, 1.0, 5 / 6))


def test_fit_box_keeps_native_aspect_untouched():
    assert fit_box(Image.new("RGB", (900, 600), "white"), (3, 2)) == (0.0, 0.0, 1.0, 1.0)


def test_fit_box_honours_centering():
    img = Image.new("RGB", (1000, 500), "white")

    left = fit_box(img, (3, 2), centering=(0.0, 0.5))
    right = fit_box(img, (3, 2), centering=(1.0, 0.5))

    assert left[0] == pytest.approx(0.0)
    assert left[2] == pytest.approx(0.75)
    assert right[2] == pytest.approx(1.0)
    assert right[0] == pytest.approx(0.25)


def test_crop_uses_fractions():
    img = Image.new("RGB", (1000, 500), "white")

    assert crop(img, (0.1, 0.0, 0.5, 1.0)).size == (400, 500)
    assert crop(img, (0.0, 0.0, 1.0, 1.0)).size == (1000, 500)


# --------------------------------------------------------------------------- to_webp


def test_to_webp_center_crops_under_3_2():
    """Из 1000×500 белая метка в центре попадает в (150,100) после кропа в 300×200."""
    url, blob, error = to_webp(load(_dot_png(1000, 500)), 300, 80)
    decoded = _decode_webp(blob)

    assert decoded.size == (300, 200)
    x, y = _bright_center(decoded)
    assert abs(x - 150) <= 1, f"ярчайший пиксель по X: {x}"
    assert abs(y - 100) <= 1, f"ярчайший пиксель по Y: {y}"
    assert 0.0 <= error <= 1.0
    assert url.startswith("data:image/webp;base64,")


def test_to_webp_size_is_exactly_3_2():
    img = Image.new("RGB", (900, 700), "white")
    for width in (300, 150):
        url, blob, _ = to_webp(img, width, 30)
        decoded = _decode_webp(blob)
        assert decoded.size[0] * 2 == decoded.size[1] * 3
        assert decoded.size == (width, round(width * 2 / 3))
        assert url == webp_data_url(blob)


def test_to_webp_height_equals_build_size_attribute():
    """Запечённая высота и `height`-атрибут слоя — одна и та же формула."""
    from bot.compare.build import _aspect_pair, _height_for

    img = Image.new("RGB", (900, 700), "white")
    for aspect in ("3:2", "4:3", "16:9", "2.66:1"):
        pair = _aspect_pair(aspect)
        for width in (203, 300):
            _, blob, _ = to_webp(img, width, 30, aspect=pair)
            assert _decode_webp(blob).size == (width, _height_for(width, aspect)), aspect


def test_to_webp_accepts_fractional_aspect_pair():
    """`2.66:1` из `bot.config` приходит уже целой парой `(133, 50)`."""
    _, blob, _ = to_webp(Image.new("RGB", (900, 700), "white"), 266, 30, aspect=(133, 50))

    assert _decode_webp(blob).size == (266, 100)


def test_to_webp_returns_data_url_and_webp_bytes():
    url, blob, error = to_webp(Image.new("RGB", (800, 600), "teal"), 200, 34)

    assert url.startswith("data:image/webp;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == blob
    assert blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"
    assert error == pytest.approx(0.0, abs=0.01)


def test_to_webp_jpeg_profile_returns_jpeg_data_url():
    url, blob, error = to_webp(Image.new("RGB", (800, 600), "teal"), 120, 40, profile="jpeg")

    assert url.startswith("data:image/jpeg;base64,")
    assert blob[:2] == b"\xff\xd8"
    assert _decode_webp(blob).size == (120, 80)
    assert 0.0 <= error <= 1.0


def test_to_webp_quant_profile_is_lossless_webp():
    url, blob, error = to_webp(Image.new("RGB", (400, 300), "teal"), 150, 30, profile="webp-quant")

    assert url.startswith("data:image/webp;base64,")
    assert blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"
    assert _decode_webp(blob).size == (150, 100)
    assert 0.0 <= error <= 0.05


def test_to_webp_soft_mode_saves_bytes_on_noise():
    noise = Image.effect_noise((400, 300), 64).convert("RGB")

    _, hard, _ = to_webp(noise, 200, 40)
    _, soft, _ = to_webp(noise, 200, 40, soft=True)

    assert len(soft) < len(hard)


def test_to_webp_default_box_equals_explicit_fit_box():
    img = load(_dot_png(1000, 500))
    box = fit_box(img, images.ASPECT)

    assert to_webp(img, 300, 60)[1] == to_webp(img, 300, 60, box=box)[1]


def test_shared_crop_box_keeps_marker_centered_in_both_sources():
    """Одинаковое окно (доли) для 2000×800 и 1200×800: центр остаётся центром."""
    source_a = load(_dot_png(2000, 800))
    source_b = load(_dot_png(1200, 800))
    box = fit_box(source_a, images.ASPECT)

    decoded_a = _decode_webp(to_webp(source_a, 300, 80, box=box)[1])
    decoded_b = _decode_webp(to_webp(source_b, 300, 80, box=box)[1])

    assert decoded_a.size == decoded_b.size == (300, 200)
    for decoded in (decoded_a, decoded_b):
        x, y = _bright_center(decoded)
        assert abs(x - 150) <= 1, f"метка по X: {x}"
        assert abs(y - 100) <= 1, f"метка по Y: {y}"


def test_shared_crop_box_maps_same_fraction_to_same_place():
    """Точка на одной и той же доле окна попадает в одно и то же место результата."""
    source_a = load(_dot_png(2000, 800, x=600))  # 0.3 ширины
    source_b = load(_dot_png(1200, 800, x=360))  # те же 0.3 ширины
    box = fit_box(source_a, images.ASPECT)  # (0.2, 0, 0.8, 1)

    decoded_a = _decode_webp(to_webp(source_a, 300, 80, box=box)[1])
    decoded_b = _decode_webp(to_webp(source_b, 300, 80, box=box)[1])

    coords = [_bright_center(decoded) for decoded in (decoded_a, decoded_b)]
    assert abs(coords[0][0] - 50) <= 1, coords
    assert abs(coords[1][0] - 50) <= 1, coords
    assert abs(coords[0][1] - coords[1][1]) <= 1


def test_to_webp_rejects_zero_width():
    with pytest.raises(ImageError):
        to_webp(Image.new("RGB", (10, 10), "white"), 0, 30)


def test_to_webp_rejects_unknown_profile():
    with pytest.raises(ImageError, match="профиль"):
        to_webp(Image.new("RGB", (10, 10), "white"), 10, 30, profile="bmp")


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
    assert _decode_webp(data).size == (32, 21)


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
