"""Тесты оригиналов для мини-аппа: WebP + AVIF, кроп как у бабла, чтение и удаление.

Pillow работает по-настоящему: картинки синтетические, диск — `tmp_path`.
AVIF-тесты пропускаются, если Pillow собран без AVIF (`features.check("avif")`).
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat, features

from bot.compare import images, originals
from bot.compare.store import ComparisonStore

ASPECT = (3, 2)
CMP_ID = "482913"


# --------------------------------------------------------------------- helpers


def picture(size: tuple[int, int] = (1200, 900)) -> Image.Image:
    """Гладкая синтетическая «фотография»: крошечная сетка, растянутая BICUBIC."""
    small = Image.new("RGB", (8, 6))
    pixels = small.load()
    for y in range(6):
        for x in range(8):
            pixels[x, y] = (x * 30, y * 40, (x * y) * 5)
    return small.resize(size, Image.BICUBIC)


def banded(size: tuple[int, int] = (900, 675), band: int = 34) -> Image.Image:
    """Картинка 4:3 с красной полосой сверху: кроп 3:2 обязан её срезать."""
    img = Image.new("RGB", size, (0, 0, 255))
    img.paste(Image.new("RGB", (size[0], band), (255, 0, 0)), (0, 0))
    return img


def decode(blob: bytes) -> Image.Image:
    with Image.open(BytesIO(blob)) as img:
        return img.convert("RGB")


def files(cache_dir, cmp_id: str = CMP_ID) -> list[str]:
    return sorted(path.name for path in (Path(cache_dir) / "originals").glob(f"{cmp_id}-*"))


def avif_required() -> None:
    if not features.check("avif"):
        pytest.skip("Pillow без AVIF — тест про AVIF не применим")


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


# ------------------------------------------------------------------------ save


def test_save_writes_webp_and_avif_for_both_sides(cache_dir: Path):
    """При поддержке AVIF пишутся оба формата обеих сторон; байты — настоящие."""
    avif_required()

    paths = originals.save(cache_dir, CMP_ID, picture(), picture((1000, 750)), width=400)

    assert set(paths) == {"a.webp", "a.avif", "b.webp", "b.avif"}
    assert files(cache_dir) == [
        f"{CMP_ID}-a.avif",
        f"{CMP_ID}-a.webp",
        f"{CMP_ID}-b.avif",
        f"{CMP_ID}-b.webp",
    ]
    for relative, saved_path in paths.items():
        assert Path(saved_path) == cache_dir / "originals" / f"{CMP_ID}-{relative}"
        assert Path(saved_path).is_file()

    webp = (cache_dir / "originals" / f"{CMP_ID}-a.webp").read_bytes()
    avif = (cache_dir / "originals" / f"{CMP_ID}-a.avif").read_bytes()
    assert webp[:4] == b"RIFF" and webp[8:12] == b"WEBP"
    assert avif[4:8] == b"ftyp" and b"avif" in avif[:32]

    # AVIF q60 не толще WebP q80 при той же ширине (в отчёт — фактические байты).
    originals.save(cache_dir, "112233", picture((800, 600)), picture((800, 600)), width=800)
    webp_size = (cache_dir / "originals" / "112233-a.webp").stat().st_size
    avif_size = (cache_dir / "originals" / "112233-a.avif").stat().st_size
    assert avif_size <= webp_size, f"AVIF {avif_size} Б против WebP {webp_size} Б"


def test_avif_absent_or_failing_falls_back_to_webp(cache_dir: Path, monkeypatch, caplog):
    """Нет AVIF-кодировщика или он упал: warning и только WebP обеих сторон."""
    avif_required()

    real_encode = originals._encode

    def flaky_encode(img, extension, quality):
        if extension == "avif":
            raise OSError("avif encoder exploded")
        return real_encode(img, extension, quality)

    monkeypatch.setattr(originals, "_encode", flaky_encode)

    with caplog.at_level(logging.WARNING, logger=originals.log.name):
        paths = originals.save(cache_dir, CMP_ID, picture(), picture(), width=200)

    assert set(paths) == {"a.webp", "b.webp"}
    assert files(cache_dir) == [f"{CMP_ID}-a.webp", f"{CMP_ID}-b.webp"]
    assert "avif" in caplog.text.lower() and "exploded" in caplog.text

    # Pillow без AVIF: два WebP-файла, AVIF не появляется даже в словаре.
    monkeypatch.setattr(originals.features, "check", lambda feature: False)
    paths = originals.save(cache_dir, "445566", picture(), picture(), width=200)
    assert set(paths) == {"a.webp", "b.webp"}
    assert files(cache_dir, "445566") == ["445566-a.webp", "445566-b.webp"]
    assert originals.read(cache_dir, "445566", "a", accept="image/avif")[1] == "webp"


def test_saved_width_is_capped_and_never_upscaled(cache_dir: Path):
    """Ширина ≤ `width`; если кроп уже уже — не растягиваем и высота из AR."""
    originals.save(cache_dir, CMP_ID, picture((900, 675)), picture((900, 675)), width=300)
    for side in originals.SIDES:
        assert decode(originals.read(cache_dir, CMP_ID, side)[0]).size == (300, 200)

    originals.save(cache_dir, "112233", picture((900, 675)), picture((900, 675)), width=2000)
    assert decode(originals.read(cache_dir, "112233", "a")[0]).size == (900, 600)


def test_crop_uses_the_same_window_as_the_widget_for_both_sides(cache_dir: Path):
    """Пиксели файла = `images.crop(img, fit_box(img_a, aspect))`, а не полный кадр.

    Окно считается по `img_a` и применяется к `b`, а не подгоняется под каждую.
    """
    source = banded()

    originals.save(cache_dir, CMP_ID, source, source, width=450)

    blob, fmt = originals.read(cache_dir, CMP_ID, "a")
    assert fmt == "webp"
    saved = decode(blob)

    window = images.fit_box(source, ASPECT)
    expected = images.crop(source, window).resize(saved.size, Image.LANCZOS)
    assert saved.size == expected.size
    # Допуск — только на потери WebP q80.
    assert max(ImageStat.Stat(ImageChops.difference(saved, expected)).mean) <= 4.0

    # Красная полоса сверху срезана кропом, хотя в исходнике это пиксель (0, 0).
    assert source.getpixel((0, 0)) == (255, 0, 0)
    top_left = saved.getpixel((0, 0))
    assert top_left[2] > 200 and top_left[0] < 60

    source_a = picture((1200, 900))
    # У b другой AR и красная полоса сверху: персональный `fit_box` срезал бы её.
    source_b = banded((600, 1200), band=120)

    originals.save(cache_dir, "778899", source_a, source_b, width=600)

    window = images.fit_box(source_a, ASPECT)
    saved_a = decode(originals.read(cache_dir, "778899", "a")[0])
    expected_a = images.crop(source_a, window).resize(saved_a.size, Image.LANCZOS)
    assert max(ImageStat.Stat(ImageChops.difference(saved_a, expected_a)).mean) <= 4.0

    saved_b = decode(originals.read(cache_dir, "778899", "b")[0])
    cropped_b = images.crop(source_b, window)
    assert saved_b.size == (min(600, cropped_b.width), 400)
    # Окно a (срез ~5.6% сверху) оставляет красную полосу b, персональное — нет.
    assert saved_b.getpixel((0, 0))[0] > 150


# ------------------------------------------------------------------------ read


def test_read_prefers_avif_when_accepted_and_falls_back_to_webp(cache_dir: Path):
    avif_required()
    originals.save(cache_dir, CMP_ID, picture(), picture())

    avif_blob, avif_format = originals.read(
        cache_dir, CMP_ID, "a", accept="image/avif,image/webp,*/*"
    )
    assert avif_format == "avif"
    assert avif_blob == (cache_dir / "originals" / f"{CMP_ID}-a.avif").read_bytes()

    for accept in ("", "image/webp", "text/html"):
        blob, fmt = originals.read(cache_dir, CMP_ID, "b", accept=accept)
        assert fmt == "webp"
        assert blob[:4] == b"RIFF"

    # AVIF на диске нет: `Accept: image/avif` всё равно получает WebP-фолбэк.
    (cache_dir / "originals" / f"{CMP_ID}-a.avif").unlink()
    blob, fmt = originals.read(cache_dir, CMP_ID, "a", accept="image/avif")
    assert fmt == "webp" and blob[:4] == b"RIFF"


def test_unknown_side_and_unsafe_id_are_rejected(cache_dir: Path):
    """Значения из токена не превращаются в путь: только `a`/`b` и цифры."""
    originals.save(cache_dir, CMP_ID, picture(), picture(), width=200)

    assert originals.read(cache_dir, CMP_ID, "c") is None
    assert originals.read(cache_dir, CMP_ID, "../a") is None
    assert originals.read(cache_dir, "../482913", "a") is None
    assert originals.read(cache_dir, "999999", "a") is None

    originals.remove(cache_dir, "../482913")
    originals.remove(cache_dir, "")
    assert files(cache_dir) != []

    with pytest.raises(ValueError):
        originals.save(cache_dir, "../482913", picture(), picture())
    with pytest.raises(ValueError):
        originals.save(cache_dir, CMP_ID, picture(), picture(), width=0)


# ---------------------------------------------------------------------- remove


def test_remove_and_remove_many_are_idempotent(cache_dir: Path):
    first = originals.save(cache_dir, CMP_ID, picture(), picture(), width=200)
    second = originals.save(cache_dir, "112233", picture(), picture(), width=200)
    stored = list((cache_dir / "originals").iterdir())
    assert len(stored) == len(first) + len(second)

    originals.remove(cache_dir, CMP_ID)
    originals.remove(cache_dir, CMP_ID)
    assert files(cache_dir, CMP_ID) == []
    assert files(cache_dir, "112233") != []

    originals.remove_many(cache_dir, ["112233", "999999"])
    originals.remove_many(cache_dir, [])
    originals.remove_many(cache_dir, None)
    assert files(cache_dir, "112233") == []
    # Сам каталог остаётся: его переиспользует следующее сравнение.
    assert (cache_dir / "originals").is_dir()


# ------------------------------------------------------------------ store ties


async def test_purge_removes_originals_together_with_record(tmp_path: Path):
    """`purge(on_removed=originals.remove)` чистит и запись, и файлы оригиналов."""
    cache_dir = tmp_path / "cache"
    store = ComparisonStore(cache_dir / "cmp.db")
    try:
        cmp_id = await store.save(
            7,
            image_a=b"RIFF-a",
            image_b=b"RIFF-b",
            width=430,
            quality=34,
            labels=("До", "После"),
            zlib_bytes=1,
        )
        originals.save(cache_dir, cmp_id, picture(), picture(), width=200)
        assert files(cache_dir, cmp_id)

        removed = await store.purge(
            ttl_seconds=0,
            max_entries=100,
            on_removed=lambda removed_id: originals.remove(cache_dir, removed_id),
        )

        assert removed == 1
        assert await store.count() == 0
        assert files(cache_dir, cmp_id) == []
    finally:
        await store.close()
