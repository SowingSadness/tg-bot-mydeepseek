"""Общая логика ответов: доступ, скачивание фото, отправка ответа."""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from .config import settings
from .deepseek import DeepSeekError, deepseek
from .state import is_thinking
from .utils import extract_image_urls, split_message, telegram_html

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
CHAT_IMAGE_PREVIEWS = 3

#: Прод Bot API: контур по умолчанию для методов и скачивания файлов.
PROD_API_BASE = "https://api.telegram.org"

#: Сколько ждём `getFile` в одном контуре. Прод иногда отвечает ~60 с — ждать нет смысла:
#: параллельно тот же запрос уходит в бета-контур, и берём тот, что ответил первым.
FILE_API_TIMEOUT = 10.0
#: Общий таймаут скачивания самого файла (фото бывают по несколько мегабайт).
FILE_DOWNLOAD_TIMEOUT = 60.0

_FILE_CHUNK_SIZE = 64 * 1024

#: Сессия для файлов Bot API: держит TLS-соединение между сообщениями.
_file_session: aiohttp.ClientSession | None = None


def is_allowed(user_id: int) -> bool:
    allowed = settings.allowed_ids
    return not allowed or user_id in allowed


async def deny(message: Message) -> None:
    await message.reply("🚫 Доступ к боту ограничен списком разрешённых пользователей.")


def _file_http() -> aiohttp.ClientSession:
    """Ленивая общая сессия для файлов Bot API (создаётся при первом фото)."""
    global _file_session
    if _file_session is None or _file_session.closed:
        _file_session = aiohttp.ClientSession()
    return _file_session


async def close_file_session() -> None:
    """Закрывает общую сессию при остановке бота. Идемпотентно."""
    global _file_session
    session, _file_session = _file_session, None
    if session is not None and not session.closed:
        await session.close()


def _method_url(base: str, token: str, method: str) -> str:
    """URL метода: `<base>/bot<token>[/test]/<method>`."""
    suffix = "/test" if settings.widget_api_test_env else ""
    return f"{base.rstrip('/')}/bot{token}{suffix}/{method}"


def _file_bases() -> tuple[str, str]:
    """Пара контуров для `getFile`: бета (в `WIDGET_API_BASE`) и прод."""
    beta = settings.widget_api_base.strip().rstrip("/") or PROD_API_BASE
    if beta == PROD_API_BASE:
        return (PROD_API_BASE,)
    return (beta, PROD_API_BASE)


async def _file_path_from(base: str, token: str, file_id: str) -> str:
    """`getFile` через конкретный контур Bot API; возвращает `file_path`."""
    url = _method_url(base, token, "getFile")
    timeout = aiohttp.ClientTimeout(total=FILE_API_TIMEOUT)
    async with _file_http().get(
        url, params={"file_id": file_id}, timeout=timeout
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"{base}: HTTP {response.status} на getFile")
        body = await response.json(content_type=None)

    path = ((body or {}).get("result") or {}).get("file_path")
    if not path:
        raise RuntimeError(f"{base}: Bot API не вернул file_path")
    return path


async def resolve_file_path(token: str, file_id: str) -> tuple[str, str]:
    """Ищет `file_path` сразу в нескольких контурах; берёт первый ответивший.

    Продовый `getFile` периодически отвечает ~60 с (таймаут апдейта Telegram),
    бета в той же сети отвечает за доли секунды — поэтому запросы идут наперегонки.
    Если все контуры ответили ошибкой, наружу уходит последняя из них.
    """
    bases = _file_bases()
    tasks = {
        asyncio.create_task(_file_path_from(base, token, file_id)): base
        for base in bases
    }
    pending = set(tasks)
    errors: list[Exception] = []
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                error = task.exception()
                if error is not None:
                    errors.append(error)
                    continue
                return task.result(), tasks[task]
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    raise errors[-1] if errors else RuntimeError("Bot API не отдал file_path")


async def _download_file_bytes(base: str, token: str, file_path: str) -> bytes:
    """Скачивает файл потоком, не превышая `MAX_DOWNLOAD_BYTES`."""
    url = f"{base.rstrip('/')}/file/bot{token}/{file_path}"
    timeout = aiohttp.ClientTimeout(total=FILE_DOWNLOAD_TIMEOUT)
    chunks: list[bytes] = []
    total = 0
    async with _file_http().get(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{base}: HTTP {response.status} на скачивании файла")
        async for chunk in response.content.iter_chunked(_FILE_CHUNK_SIZE):
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"{base}: файл больше {MAX_DOWNLOAD_BYTES} байт")
            chunks.append(chunk)
    return b"".join(chunks)


async def download_photo(message: Message, file_id: str) -> bytes:
    """Скачивает фото/документ сообщения, устойчиво к медленному `getFile`.

    `file_path` берётся у того контура, который ответил первым, а если файл
    в нём не отдался (контуры нумеруют файлы по-разному) — пробуем другой.
    """
    token = message.bot.token
    file_path, base = await resolve_file_path(token, file_id)
    try:
        return await _download_file_bytes(base, token, file_path)
    except Exception as exc:  # noqa: BLE001 — пробуем резервный контур
        fallback = PROD_API_BASE if base != PROD_API_BASE else _file_bases()[0]
        if fallback == base:
            raise
        log.info("Файл не скачался через %s (%s) — пробую %s", base, exc, fallback)

    path = await _file_path_from(fallback, token, file_id)
    return await _download_file_bytes(fallback, token, path)


async def ask_and_reply(
    message: Message,
    prompt: str,
    images: list[str] | None = None,
) -> None:
    """Отправляет запрос в DeepSeek и публикует ответ в чат."""
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    thinking = is_thinking(message.from_user.id)
    started = time.perf_counter()
    try:
        answer = await deepseek.ask(
            prompt,
            images=images,
            thinking=thinking,
            max_tokens=settings.chat_max_tokens or None,
        )
    except DeepSeekError as exc:
        log.info(
            "chat user=%d: error total=%.2fs %s",
            message.from_user.id, time.perf_counter() - started, exc,
        )
        await message.reply(f"⚠️ {exc}")
        return

    log.info(
        "chat user=%d: total=%.2fs chars=%d images=%d thinking=%s",
        message.from_user.id, time.perf_counter() - started, len(answer),
        len(images or []), thinking,
    )

    chunks = split_message(answer)
    for index, chunk in enumerate(chunks):
        content, parse_mode = telegram_html(chunk)
        extra: dict = {}
        if index == 0:
            extra["reply_to_message_id"] = message.message_id
        try:
            await message.answer(content, parse_mode=parse_mode, **extra)
        except TelegramBadRequest:
            await message.answer(chunk, parse_mode=None, **extra)

    for url in extract_image_urls(answer, limit=CHAT_IMAGE_PREVIEWS):
        try:
            await message.answer_photo(url)
        except TelegramBadRequest:
            log.info("Не удалось отправить картинку %s", url)
