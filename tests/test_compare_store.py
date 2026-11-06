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


async def test_aspect_round_trips(store: ComparisonStore):
    cmp_id = await _save(store, aspect="4:3")

    cached = await store.get(cmp_id)

    assert cached is not None
    assert cached.aspect == "4:3"


async def test_get_filters_by_owner(store: ComparisonStore):
    cmp_id = await _save(store, owner_id=7)

    assert await store.get(cmp_id, owner_id=8) is None
    assert await store.get(cmp_id, owner_id=7) is not None

    shared = await store.get(cmp_id, owner_id=None)
    assert shared is not None
    assert shared.owner_id == 7
    assert shared.image_b == IMAGE_B


async def test_get_unknown_id_returns_none(store: ComparisonStore):
    assert await store.get("000000") is None
    assert await store.get("000000", owner_id=7) is None


async def test_preview_file_id_defaults_to_none_and_round_trips(store: ComparisonStore):
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


async def test_set_preview_file_id_updates_existing_record(store: ComparisonStore):
    cmp_id = await _save(store)

    await store.set_preview_file_id(cmp_id, "file-id-after-upload")
    cached = await store.get(cmp_id)

    assert cached is not None
    assert cached.preview_file_id == "file-id-after-upload"


async def test_set_preview_file_id_survives_reopen(tmp_path: Path):
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


async def test_set_preview_file_id_for_unknown_id_is_noop(store: ComparisonStore):
    """Номер уже вычищен purge-ем — превью просто некуда писать, но и падать незачем."""
    await store.set_preview_file_id("000000", "orphan-file-id")

    assert await store.count() == 0
    assert await store.get("000000") is None


async def test_purge_zero_ttl_removes_everything(store: ComparisonStore):
    for index in range(3):
        await _save(store, now=100.0 + index)

    assert await store.count() == 3

    # Реальные часы далеко впереди now=100..102, поэтому TTL=0 удаляет всё.
    removed = await store.purge(ttl_seconds=0, max_entries=100)

    assert removed == 3
    assert await store.count() == 0


async def test_purge_calls_on_removed_for_each_deleted_id(store: ComparisonStore):
    """`on_removed` получает каждый удалённый номер: по нему чистятся файлы оригиналов."""
    ids = [await _save(store, now=100.0 + index) for index in range(3)]
    seen: list[str] = []

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


async def test_id_collision_grows_number_after_ten_tries(store: ComparisonStore, monkeypatch):
    monkeypatch.setattr("secrets.randbelow", lambda _bound: 0)
    first = await _save(store)
    assert first == "100000"

    # 10 конфликтов на 6 цифрах → номер становится семизначным.
    second = await _save(store)

    assert second == "1000000"
    assert len(second) == 7
    assert await store.count() == 2


async def test_save_defaults_aspect_to_3_2(store: ComparisonStore):
    """Без явного `aspect` новая запись получает дефолт 3:2 (текущий AR бабла)."""
    cmp_id = await store.save(
        7,
        image_a=IMAGE_A,
        image_b=IMAGE_B,
        width=430,
        quality=34,
        labels=LABELS,
        zlib_bytes=1024,
    )

    cached = await store.get(cmp_id)

    assert cached is not None
    assert isinstance(cached.aspect, str)
    assert cached.aspect == "3:2"


async def test_migrates_old_database_without_aspect(tmp_path: Path):
    """Старая БД без колонки `aspect`: ALTER TABLE без заполнения, чтение → 4:3."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE comparisons (
              id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, created_at REAL NOT NULL,
              width INTEGER NOT NULL, quality INTEGER NOT NULL, labels TEXT NOT NULL,
              zlib_bytes INTEGER NOT NULL, image_a BLOB NOT NULL, image_b BLOB NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO comparisons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("482913", 7, 100.0, 430, 34, "До|После", 1024, IMAGE_A, IMAGE_B),
        )
        conn.commit()
    finally:
        conn.close()

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


async def test_migrates_old_database_without_preview_file_id(tmp_path: Path):
    """Старая БД без `preview_file_id`: ALTER TABLE, старые строки читаются с NULL."""
    path = tmp_path / "old-preview.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE comparisons (
              id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, created_at REAL NOT NULL,
              width INTEGER NOT NULL, quality INTEGER NOT NULL, labels TEXT NOT NULL,
              zlib_bytes INTEGER NOT NULL, image_a BLOB NOT NULL, image_b BLOB NOT NULL,
              aspect TEXT NOT NULL DEFAULT '3:2'
            );
            """
        )
        conn.execute(
            "INSERT INTO comparisons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("482913", 7, 100.0, 430, 34, "До|После", 1024, IMAGE_A, IMAGE_B, "3:2"),
        )
        conn.commit()
    finally:
        conn.close()

    store = ComparisonStore(path)
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


async def test_creates_missing_directories(tmp_path: Path):
    path = tmp_path / "a" / "b" / "cmp.db"

    store = ComparisonStore(path)
    try:
        assert path.exists()
        cmp_id = await _save(store)
        assert await store.get(cmp_id) is not None
    finally:
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


async def test_count_and_close_are_idempotent(tmp_path: Path):
    store = ComparisonStore(tmp_path / "cmp.db")
    try:
        assert await store.count() == 0
        assert await store.count() == 0
        await _save(store)
        assert await store.count() == 1
    finally:
        await store.close()
        await store.close()
