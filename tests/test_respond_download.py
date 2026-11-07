"""Скачивание файлов Bot API: гонка контуров и резервный `getFile`.

Сеть не нужна: `_file_path_from` / `_download_file_bytes` подменяются фейками.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import respond

BETA = "https://api.telegram.org/beta"
PROD = "https://api.telegram.org"


def _message() -> SimpleNamespace:
    """Минимальный «message» — `download_photo` читает только `bot.token`."""
    return SimpleNamespace(bot=SimpleNamespace(token="123:test"))


def test_bases_are_beta_then_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(respond.settings, "widget_api_base", BETA)
    assert respond._file_bases() == (BETA, PROD)

    monkeypatch.setattr(respond.settings, "widget_api_base", PROD + "/")
    assert respond._file_bases() == (PROD,)


async def test_resolve_file_path_takes_first_falls_back_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Медленный прод не задерживает ответ; если бета не знает файл — берём прод."""

    async def fake(base: str, token: str, file_id: str) -> str:
        if base == PROD:
            await asyncio.sleep(30)
        return f"{base}/path"

    monkeypatch.setattr(respond, "_file_path_from", fake)
    monkeypatch.setattr(respond.settings, "widget_api_base", BETA)

    assert await respond.resolve_file_path("123:test", "fid") == (f"{BETA}/path", BETA)

    async def fallback(base: str, token: str, file_id: str) -> str:
        if base == BETA:
            raise RuntimeError("beta: HTTP 404 на getFile")
        return "prod/path"

    monkeypatch.setattr(respond, "_file_path_from", fallback)
    assert await respond.resolve_file_path("123:test", "fid") == ("prod/path", PROD)

    async def broken(base: str, token: str, file_id: str) -> str:
        raise RuntimeError(f"{base}: нет связи")

    monkeypatch.setattr(respond, "_file_path_from", broken)
    with pytest.raises(RuntimeError, match="нет связи"):
        await respond.resolve_file_path("123:test", "fid")


async def test_download_photo_returns_bytes_and_switches_contour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Прод «медленный» — побеждает бета; файл у беты битый — путь берём у прода."""

    async def fake_path(base: str, token: str, file_id: str) -> str:
        if base == PROD:
            await asyncio.sleep(0.05)
        return "photos/file_1.jpg"

    downloaded: list[tuple[str, str]] = []

    async def fake_bytes(base: str, token: str, file_path: str) -> bytes:
        downloaded.append((base, file_path))
        return b"jpeg"

    monkeypatch.setattr(respond, "_file_path_from", fake_path)
    monkeypatch.setattr(respond, "_download_file_bytes", fake_bytes)
    monkeypatch.setattr(respond.settings, "widget_api_base", BETA)

    assert await respond.download_photo(_message(), "fid") == b"jpeg"
    assert downloaded == [(BETA, "photos/file_1.jpg")]

    # Контуры нумеруют файлы по-разному: путь перезапрашивается у резерва.
    async def per_contour_path(base: str, token: str, file_id: str) -> str:
        if base == PROD:
            await asyncio.sleep(0.05)
        return "beta/file_1" if base == BETA else "prod/file_7.jpg"

    async def beta_404(base: str, token: str, file_path: str) -> bytes:
        if base == BETA:
            raise RuntimeError(f"{base}: HTTP 404 на скачивании файла")
        return b"jpeg"

    monkeypatch.setattr(respond, "_file_path_from", per_contour_path)
    monkeypatch.setattr(respond, "_download_file_bytes", beta_404)
    assert await respond.download_photo(_message(), "fid") == b"jpeg"


async def test_download_photo_keeps_error_without_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Один контур (бета = прод) — резерва нет, ошибка уходит наверх."""

    async def fake_path(base: str, token: str, file_id: str) -> str:
        return "photos/file_1.jpg"

    async def fake_bytes(base: str, token: str, file_path: str) -> bytes:
        raise RuntimeError("контур недоступен")

    monkeypatch.setattr(respond, "_file_path_from", fake_path)
    monkeypatch.setattr(respond, "_download_file_bytes", fake_bytes)
    monkeypatch.setattr(respond.settings, "widget_api_base", PROD)

    with pytest.raises(RuntimeError, match="контур недоступен"):
        await respond.download_photo(_message(), "fid")
