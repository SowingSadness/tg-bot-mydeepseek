"""Тесты дискового кеша сравнений (SQLite). Без сети, время передаётся явно."""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

import pytest

from bot.compare.store import Cached, ComparisonStore

LABELS = ("До", "После")
IMAGE_A = b"\x00\x01RIFF-a\xff\xfe"
IMAGE_B = b"\x02\x03RIFF-b\x00\x00"


@pytest.fixture
async def store(tmp_path: Path):
    """Свежий кеш на каждый тест; закрывается даже при падении теста."""
    instance = ComparisonStore(tmp_path / "cmp.db")
    try:
        yield instance
    finally:
        await instance.close()


async def _save(
    store: ComparisonStore,
    *,
    owner_id: int = 7,
    now: float | None = None,
    image_a: bytes = IMAGE_A,
    image_b: bytes = IMAGE_B,
    aspect: str = "3:2",
) -> str:
    return await store.save(
        owner_id,
        image_a=image_a,
        image_b=image_b,
        width=430,
        quality=34,
        labels=LABELS,
        zlib_bytes=1024,
        aspect=aspect,
        now=now,
    )


async def test_save_returns_six_digit_id(store: ComparisonStore):
    cmp_id = await _save(store)

    assert re.fullmatch(r"\d{6}", cmp_id)


async def test_reopen_finds_identical_record(tmp_path: Path):
    path = tmp_path / "cmp.db"
    first = ComparisonStore(path)
    try:
        cmp_id = await _save(first, owner_id=42, now=1234.5)
    finally:
        await first.close()

    second = ComparisonStore(path)
    try:
        cached = await second.get(cmp_id)
    finally:
        await second.close()

    assert isinstance(cached, Cached)
    assert cached.id == cmp_id
    assert cached.owner_id == 42
    assert cached.created_at == 1234.5
    assert cached.width == 430
    assert cached.quality == 34
    assert cached.labels == LABELS
    assert cached.zlib_bytes == 1024
    assert cached.image_a == IMAGE_A
    assert cached.image_b == IMAGE_B
    assert cached.aspect == "3:2"


async def test_aspect_round_trips_and_defaults_to_3_2(store: ComparisonStore):
    cmp_id = await _save(store, aspect="4:3")
    cached = await store.get(cmp_id)
    assert cached is not None
    assert cached.aspect == "4:3"

    # Без явного `aspect` новая запись получает дефолт 3:2 (текущий AR бабла).
    default_id = await store.save(
        7,
        image_a=IMAGE_A,
        image_b=IMAGE_B,
        width=430,
        quality=34,
        labels=LABELS,
        zlib_bytes=1024,
    )
    cached = await store.get(default_id)
    assert cached is not None
    assert isinstance(cached.aspect, str)
    assert cached.aspect == "3:2"


async def test_get_filters_by_owner_and_unknown_id(store: ComparisonStore):
    cmp_id = await _save(store, owner_id=7)

    assert await store.get(cmp_id, owner_id=8) is None
    assert await store.get(cmp_id, owner_id=7) is not None

    shared = await store.get(cmp_id, owner_id=None)
    assert shared is not None
    assert shared.owner_id == 7
    assert shared.image_b == IMAGE_B

    assert await store.get("000000") is None
    assert await store.get("000000", owner_id=7) is None


async def test_preview_file_id_round_trip_update_and_noop(store: ComparisonStore):
    """`preview_file_id` — необязательное украшение: без него NULL, с ним — сохраняется."""
    plain_id = await _save(store)
    plain = await store.get(plain_id)
    assert plain is not None and plain.preview_file_id is None

    with_preview = await store.save(
        7,
        image_a=IMAGE_A,
        image_b=IMAGE_B,
        width=430,
        quality=34,
        labels=LABELS,
        zlib_bytes=1024,
        preview_file_id="AgACAgIAAxkBAAI",
    )
    cached = await store.get(with_preview)
    assert cached is not None
    assert cached.preview_file_id == "AgACAgIAAxkBAAI"

    await store.set_preview_file_id(plain_id, "file-id-after-upload")
    updated = await store.get(plain_id)
    assert updated is not None
    assert updated.preview_file_id == "file-id-after-upload"

    # Номер уже вычищен purge-ем — превью просто некуда писать, но и падать незачем.
    before_noop = await store.count()
    await store.set_preview_file_id("000000", "orphan-file-id")
    assert await store.count() == before_noop
    assert await store.get("000000") is None


async def test_preview_file_id_survives_reopen(tmp_path: Path):
    path = tmp_path / "cmp.db"
    first = ComparisonStore(path)
    try:
        cmp_id = await _save(first)
        await first.set_preview_file_id(cmp_id, "persisted-file-id")
    finally:
        await first.close()

    second = ComparisonStore(path)
    try:
        cached = await second.get(cmp_id)
    finally:
        await second.close()

    assert cached is not None
    assert cached.preview_file_id == "persisted-file-id"


async def test_purge_zero_ttl_removes_everything_and_calls_on_removed(
    store: ComparisonStore,
):
    """`on_removed` получает каждый удалённый номер: по нему чистятся файлы оригиналов."""
    ids = [await _save(store, now=100.0 + index) for index in range(3)]
    assert await store.count() == 3
    seen: list[str] = []

    # Реальные часы далеко впереди now=100..102, поэтому TTL=0 удаляет всё.
    removed = await store.purge(ttl_seconds=0, max_entries=100, on_removed=seen.append)

    assert removed == 3
    assert sorted(seen) == sorted(ids)
    assert await store.count() == 0


async def test_purge_survives_callback_error(store: ComparisonStore, caplog):
    """Исключение в `on_removed` — warning, purge дочищает остальные и не падает."""
    ids = [await _save(store, now=100.0 + index) for index in range(3)]
    seen: list[str] = []

    def boom(cmp_id: str) -> None:
        seen.append(cmp_id)
        raise RuntimeError("disk is on fire")

    with caplog.at_level(logging.WARNING, logger="bot.compare.store"):
        removed = await store.purge(ttl_seconds=0, max_entries=100, on_removed=boom)

    assert removed == 3
    assert sorted(seen) == sorted(ids)
    assert "disk is on fire" in caplog.text
    assert await store.count() == 0


async def test_purge_keeps_newest_entries(store: ComparisonStore):
    ids = [await _save(store, now=float(index + 1)) for index in range(5)]

    # TTL заведомо больше возраста записей — проверяем только лимит количества.
    removed = await store.purge(ttl_seconds=10**12, max_entries=3)

    assert removed == 2
    assert await store.get(ids[0]) is None
    assert await store.get(ids[1]) is None
    for cmp_id in ids[2:]:
        assert await store.get(cmp_id) is not None


async def test_id_collision_retries_with_new_number(store: ComparisonStore, monkeypatch):
    monkeypatch.setattr("secrets.randbelow", lambda _bound: 0)
    first = await _save(store)
    assert first == "100000"

    # Первые две попытки дают уже занятый номер, третья — свободный.
    values = iter([0, 0, 12345])
    monkeypatch.setattr("secrets.randbelow", lambda _bound: next(values))

    second = await _save(store, image_a=b"second")

    assert second == "112345"
    assert second != first
    assert await store.count() == 2
    cached = await store.get(first)
    assert cached is not None and cached.image_a == IMAGE_A


async def test_id_collision_grows_number_after_ten_tries(
    store: ComparisonStore, monkeypatch
):
    monkeypatch.setattr("secrets.randbelow", lambda _bound: 0)
    first = await _save(store)
    assert first == "100000"

    # 10 конфликтов на 6 цифрах → номер становится семизначным.
    second = await _save(store)

    assert second == "1000000"
    assert len(second) == 7
    assert await store.count() == 2


def _old_database(path: Path, columns: str, values: tuple) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(f"CREATE TABLE comparisons ({columns});")
        conn.execute(
            "INSERT INTO comparisons VALUES (" + ", ".join("?" * len(values)) + ")",
            values,
        )
        conn.commit()
    finally:
        conn.close()


async def test_migrates_old_database_columns(tmp_path: Path):
    """Старые БД без `aspect`/`preview_file_id`: ALTER TABLE и понятные дефолты."""
    path = tmp_path / "old.db"
    _old_database(
        path,
        """
        id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, created_at REAL NOT NULL,
        width INTEGER NOT NULL, quality INTEGER NOT NULL, labels TEXT NOT NULL,
        zlib_bytes INTEGER NOT NULL, image_a BLOB NOT NULL, image_b BLOB NOT NULL
        """,
        ("482913", 7, 100.0, 430, 34, "До|После", 1024, IMAGE_A, IMAGE_B),
    )

    store = ComparisonStore(path)
    try:
        cached = await store.get("482913")
        assert cached is not None
        # Миграция не выдумывает AR: старые строки запечены в 4:3 и читаются так же.
        assert cached.aspect == "4:3"
        assert isinstance(cached.aspect, str)
        assert cached.image_a == IMAGE_A

        # Новые записи без aspect берут дефолт save → 3:2.
        fresh_id = await store.save(
            7,
            image_a=IMAGE_A,
            image_b=IMAGE_B,
            width=430,
            quality=34,
            labels=LABELS,
            zlib_bytes=1024,
        )
        fresh = await store.get(fresh_id)
        assert fresh is not None and fresh.aspect == "3:2"

        # После миграции явный aspect тоже работает.
        legacy_id = await _save(store, aspect="4:3")
        legacy = await store.get(legacy_id)
        assert legacy is not None and legacy.aspect == "4:3"
    finally:
        await store.close()

    preview_path = tmp_path / "old-preview.db"
    _old_database(
        preview_path,
        """
        id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, created_at REAL NOT NULL,
        width INTEGER NOT NULL, quality INTEGER NOT NULL, labels TEXT NOT NULL,
        zlib_bytes INTEGER NOT NULL, image_a BLOB NOT NULL, image_b BLOB NOT NULL,
        aspect TEXT NOT NULL DEFAULT '3:2'
        """,
        ("482913", 7, 100.0, 430, 34, "До|После", 1024, IMAGE_A, IMAGE_B, "3:2"),
    )

    store = ComparisonStore(preview_path)
    try:
        cached = await store.get("482913")
        assert cached is not None
        # Старые записи превью не имеют — inline просто не покажет фото.
        assert cached.preview_file_id is None
        assert cached.image_a == IMAGE_A

        # Миграция не мешает ни UPDATE старой строки, ни новым вставкам с file_id.
        await store.set_preview_file_id("482913", "legacy-then-uploaded")
        again = await store.get("482913")
        assert again is not None and again.preview_file_id == "legacy-then-uploaded"

        fresh_id = await store.save(
            7,
            image_a=IMAGE_A,
            image_b=IMAGE_B,
            width=430,
            quality=34,
            labels=LABELS,
            zlib_bytes=1024,
            preview_file_id="fresh-file-id",
        )
        fresh = await store.get(fresh_id)
        assert fresh is not None and fresh.preview_file_id == "fresh-file-id"
    finally:
        await store.close()


async def test_store_lifecycle_creates_dirs_and_is_idempotent(tmp_path: Path):
    path = tmp_path / "a" / "b" / "cmp.db"

    store = ComparisonStore(path)
    try:
        assert path.exists()
        assert await store.count() == 0
        assert await store.count() == 0
        cmp_id = await _save(store)
        assert await store.get(cmp_id) is not None
        assert await store.count() == 1
    finally:
        await store.close()
        await store.close()


async def test_labels_round_trip_as_tuple(store: ComparisonStore):
    cmp_id = await store.save(
        7,
        image_a=IMAGE_A,
        image_b=IMAGE_B,
        width=320,
        quality=20,
        labels=("before", "after"),
        zlib_bytes=999,
    )

    cached = await store.get(cmp_id)

    assert cached is not None
    assert isinstance(cached.labels, tuple)
    assert cached.labels == ("before", "after")
