"""Дисковый кеш сравнений: номер `#482913` → сжатые WebP + метаданные.

В кеше лежит **ровно то, что нужно, чтобы пересобрать бабл**: два WebP (без base64)
и параметры сборки. HTML не хранится — он детерминированно пересобирается через
`render_cached` за миллисекунды, поэтому inline-режим укладывается в свой дедлайн.

Модуль не импортирует `bot.config`: путь, длина номера и все лимиты приходят
аргументами. Все обращения к sqlite3 уходят в `asyncio.to_thread`, записи
сериализуются `asyncio.Lock` (соединение открыто с `check_same_thread=False`).
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS comparisons (
  id         TEXT PRIMARY KEY,
  owner_id   INTEGER NOT NULL,
  created_at REAL    NOT NULL,
  width      INTEGER NOT NULL,
  quality    INTEGER NOT NULL,
  labels     TEXT    NOT NULL,
  zlib_bytes INTEGER NOT NULL,
  image_a    BLOB    NOT NULL,
  image_b    BLOB    NOT NULL,
  aspect     TEXT    NOT NULL DEFAULT '3:2',
  preview_file_id TEXT
);
CREATE INDEX IF NOT EXISTS comparisons_owner ON comparisons(owner_id, created_at DESC);
"""

_COLUMNS = (
    "id, owner_id, created_at, width, quality, labels, zlib_bytes, image_a, image_b, aspect,"
    " preview_file_id"
)


@dataclass(frozen=True)
class Cached:
    """Одно сохранённое сравнение: номер, владелец и всё для `render_cached`."""

    id: str
    owner_id: int
    created_at: float
    width: int
    quality: int
    labels: tuple[str, str]
    zlib_bytes: int
    image_a: bytes
    image_b: bytes
    aspect: str = "3:2"
    #: `file_id` композита-превью из Telegram; `None` — ещё не загружали (или старая запись).
    preview_file_id: str | None = None


class ComparisonStore:
    """SQLite-кеш сравнений с номерами фиксированной длины.

    Соединение одно на процесс; пишущие операции (`save`, `purge`, `close`) идут
    через `asyncio.Lock`, а сами вызовы sqlite3 — через `asyncio.to_thread`.
    """

    def __init__(self, path: Path, *, id_digits: int = 6) -> None:
        if id_digits < 1:
            raise ValueError("id_digits должен быть положительным")
        self._path = Path(path)
        self._id_digits = int(id_digits)
        self._lock = asyncio.Lock()

        # Каталог кеша может отсутствовать (первый запуск, свежий volume).
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self._path), check_same_thread=False
        )
        # WAL — читатели не блокируют писателя (важно для inline), NORMAL — дешевле fsync.
        self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate_aspect(self._conn)
        self._migrate_preview_file_id(self._conn)
        self._conn.commit()

    @staticmethod
    def _migrate_aspect(conn: sqlite3.Connection) -> None:
        """Добавляет колонку `aspect` в старую БД, не заполняя её.

        Старые записи запечены в 4:3, поэтому при чтении NULL превращается
        в `"4:3"` (прежний дефолт): только так `render_cached` пересоберёт
        старый `#номер` в его настоящих пропорциях.
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(comparisons)")}
        if "aspect" not in columns:
            conn.execute("ALTER TABLE comparisons ADD COLUMN aspect TEXT")

    @staticmethod
    def _migrate_preview_file_id(conn: sqlite3.Connection) -> None:
        """Добавляет колонку `preview_file_id` в старую БД; старые строки — NULL.

        Заполнять нечем: `file_id` появляется только после реальной загрузки
        композита в Telegram, поэтому inline просто не покажет фото для старых
        номеров, пока их не пересоберут.
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(comparisons)")}
        if "preview_file_id" not in columns:
            conn.execute("ALTER TABLE comparisons ADD COLUMN preview_file_id TEXT")

    # ------------------------------------------------------------------ helpers

    @property
    def path(self) -> Path:
        """Путь к файлу БД (полезно для логов и тестов)."""
        return self._path

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ComparisonStore уже закрыт")
        return self._conn

    def _new_id(self, digits: int) -> str:
        """Случайный номер ровно из `digits` цифр (первая — не ноль)."""
        low = 10 ** (digits - 1)
        return str(secrets.randbelow(10**digits - low) + low)

    def _insert(
        self,
        cmp_id: str,
        owner_id: int,
        created_at: float,
        width: int,
        quality: int,
        labels: str,
        zlib_bytes: int,
        image_a: bytes,
        image_b: bytes,
        aspect: str,
        preview_file_id: str | None,
    ) -> None:
        """Одна попытка INSERT; при конфликте откатывает транзакцию."""
        conn = self._connection()
        try:
            conn.execute(
                f"INSERT INTO comparisons ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cmp_id,
                    owner_id,
                    created_at,
                    width,
                    quality,
                    labels,
                    zlib_bytes,
                    image_a,
                    image_b,
                    aspect,
                    preview_file_id,
                ),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _select(self, cmp_id: str, owner_id: int | None) -> tuple | None:
        conn = self._connection()
        sql = f"SELECT {_COLUMNS} FROM comparisons WHERE id = ?"
        params: list[object] = [cmp_id]
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params.append(owner_id)
        return conn.execute(sql, params).fetchone()

    def _purge(self, ttl_seconds: float, max_entries: int, now: float) -> int:
        """Удаляет устаревшее и лишнее одной транзакцией (один поток)."""
        conn = self._connection()
        try:
            expired = conn.execute(
                "DELETE FROM comparisons WHERE created_at < ?",
                (now - ttl_seconds,),
            ).rowcount
            extra = conn.execute(
                "DELETE FROM comparisons WHERE id IN ("
                "  SELECT id FROM comparisons"
                "  ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?"
                ")",
                (max(0, int(max_entries)),),
            ).rowcount
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return int(expired) + int(extra)

    def _count(self) -> int:
        row = self._connection().execute("SELECT COUNT(*) FROM comparisons").fetchone()
        return int(row[0])

    def _set_preview_file_id(self, cmp_id: str, file_id: str) -> None:
        """UPDATE по номеру; несуществующий номер — тихо ничего не делает."""
        conn = self._connection()
        try:
            conn.execute(
                "UPDATE comparisons SET preview_file_id = ? WHERE id = ?",
                (str(file_id), str(cmp_id)),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        conn.close()

    # --------------------------------------------------------------------- API

    async def save(
        self,
        owner_id: int,
        *,
        image_a: bytes,
        image_b: bytes,
        width: int,
        quality: int,
        labels: tuple[str, str],
        zlib_bytes: int,
        aspect: str = "3:2",
        preview_file_id: str | None = None,
        now: float | None = None,
    ) -> str:
        """Сохраняет сравнение и возвращает свободный номер (строкой).

        Коллизия — это не ошибка: до 10 попыток на текущей длине, затем номер
        становится на одну цифру длиннее.
        """
        created_at = time.time() if now is None else float(now)
        labels_text = "|".join(labels)
        digits = self._id_digits
        attempts = 0

        async with self._lock:
            while True:
                cmp_id = self._new_id(digits)
                try:
                    await asyncio.to_thread(
                        self._insert,
                        cmp_id,
                        owner_id,
                        created_at,
                        width,
                        quality,
                        labels_text,
                        zlib_bytes,
                        image_a,
                        image_b,
                        str(aspect),
                        None if preview_file_id is None else str(preview_file_id),
                    )
                except sqlite3.IntegrityError:
                    attempts += 1
                    if attempts >= 10:
                        digits += 1
                        attempts = 0
                    continue
                return cmp_id

    async def get(self, cmp_id: str, *, owner_id: int | None = None) -> Cached | None:
        """Ищет сравнение по номеру; `owner_id` ограничивает выдачу владельцем."""
        row = await asyncio.to_thread(self._select, str(cmp_id), owner_id)
        if row is None:
            return None
        first, second = str(row[5]).split("|", 1)
        return Cached(
            id=row[0],
            owner_id=row[1],
            created_at=row[2],
            width=row[3],
            quality=row[4],
            labels=(first, second),
            zlib_bytes=row[6],
            image_a=row[7],
            image_b=row[8],
            # NULL — запись из старой БД: она запечена в 4:3, это прежний дефолт.
            aspect=str(row[9] or "4:3"),
            preview_file_id=None if row[10] is None else str(row[10]),
        )

    async def set_preview_file_id(self, cmp_id: str, file_id: str) -> None:
        """Запоминает `file_id` композита-превью для номера `cmp_id`.

        Прежние номера, которых уже нет в кеше, не ошибка: превью — украшение,
        поэтому UPDATE просто ничего не меняет.
        """
        async with self._lock:
            await asyncio.to_thread(self._set_preview_file_id, str(cmp_id), str(file_id))

    async def purge(self, *, ttl_seconds: float, max_entries: int) -> int:
        """Удаляет записи старше TTL и лишние сверх `max_entries`.

        Оставляет самые свежие по `created_at`. Возвращает число удалённых записей.
        """
        now = time.time()
        async with self._lock:
            return await asyncio.to_thread(
                self._purge, float(ttl_seconds), int(max_entries), now
            )

    async def count(self) -> int:
        """Сколько сравнений сейчас в кеше."""
        return await asyncio.to_thread(self._count)

    async def close(self) -> None:
        """Закрывает соединение; повторный вызов ничего не делает."""
        async with self._lock:
            await asyncio.to_thread(self._close)
