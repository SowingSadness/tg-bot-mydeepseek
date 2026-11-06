"""Тонкий клиент Bot API 10.4: `sendWidget` и `answerInlineQuery` с виджетом.

Сеть изолирована за `Transport`: в тестах подставляется фейковая функция, реальные
запросы к `api.telegram.org` не уходят. `aiohttp.ClientSession` создаётся лениво
только для скачивания картинок (`session`), поэтому HTTP-вызовы API её не требуют.

Модуль не импортирует `bot.config`: токен, база и таймауты приходят аргументами.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import aiohttp

# Сколько раз повторяем запрос, если сервер вернул `parameters.retry_after`.
MAX_RETRIES = 2

#: Транспорт: `(url, json_payload) -> (http_status, json_payload)`.
Transport = Callable[[str, dict], Awaitable[tuple[int, dict]]]


class WidgetAPIError(Exception):
    """Ошибка Bot API с кодом/описанием и (если просили) задержкой повтора."""

    def __init__(
        self,
        code: int | None,
        description: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.retry_after = retry_after

    def __str__(self) -> str:
        code = "без кода" if self.code is None else f"код {self.code}"
        text = f"Ошибка Bot API ({code}): {self.description}"
        if self.retry_after is not None:
            text += f" (retry_after={self.retry_after:g})"
        return text


class WidgetAPI:
    """Клиент методов Bot API 10.4, нужных для HTML-баблов сравнения."""

    def __init__(
        self,
        token: str,
        base: str = "https://api.telegram.org/beta",
        test_env: bool = False,
        timeout: float = 15.0,
        transport: Transport | None = None,
    ) -> None:
        self.token = token
        self.base = base
        self.test_env = test_env
        self.timeout = timeout
        self._transport = transport
        self._session: aiohttp.ClientSession | None = None

    def url(self, method: str) -> str:
        """URL метода: `<base>/bot<token>[/test]/<method>`."""
        suffix = "/test" if self.test_env else ""
        return f"{self.base.rstrip('/')}/bot{self.token}{suffix}/{method}"

    @property
    def session(self) -> aiohttp.ClientSession:
        """Сессия для `images.download_url`: создаётся при первом обращении."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        """Закрывает ленивую сессию; без сессии ничего не создаёт. Идемпотентно."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def _post(self, url: str, payload: dict) -> tuple[int, Any]:
        """POST JSON через внедрённый транспорт или одноразовую aiohttp-сессию."""
        if self._transport is not None:
            try:
                return await self._transport(url, payload)
            except WidgetAPIError:
                raise
            except asyncio.TimeoutError as exc:
                raise WidgetAPIError(
                    None, "Bot API не ответил вовремя — попробуйте ещё раз."
                ) from exc
            except (aiohttp.ClientError, OSError) as exc:
                raise WidgetAPIError(
                    None, f"Не удалось связаться с Bot API: {exc}"
                ) from exc

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    status = response.status
                    try:
                        # content_type=None: бета может отдать text/html на 5xx/404.
                        body = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        body = None
                    return status, body
        except asyncio.TimeoutError as exc:
            raise WidgetAPIError(
                None, "Bot API не ответил вовремя — попробуйте ещё раз."
            ) from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise WidgetAPIError(
                None, f"Не удалось связаться с Bot API: {exc}"
            ) from exc

    @staticmethod
    def _parse(method: str, status: int, payload: Any) -> Any:
        """`{ok: true, result}` → result; всё остальное → `WidgetAPIError`."""
        if not isinstance(payload, dict):
            raise WidgetAPIError(
                None,
                f"Bot API вернул не JSON в ответ на {method} (HTTP {status}).",
            )

        if payload.get("ok") is True:
            return payload.get("result")

        code = payload.get("error_code")
        if isinstance(code, bool) or not isinstance(code, int):
            code = None

        description = payload.get("description")
        if not isinstance(description, str) or not description:
            description = (
                f"Bot API вернул ошибку без описания в ответ на {method} (HTTP {status})."
            )

        retry_after: float | None = None
        parameters = payload.get("parameters")
        if isinstance(parameters, dict):
            raw = parameters.get("retry_after")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                retry_after = max(0.0, float(raw))

        raise WidgetAPIError(code, description, retry_after=retry_after)

    async def call(self, method: str, **params: Any) -> Any:
        """Вызов метода: POST JSON, разбор ответа, `retry_after` ≤ `MAX_RETRIES` повторов."""
        url = self.url(method)
        retries = 0
        while True:
            status, payload = await self._post(url, params)
            try:
                return self._parse(method, status, payload)
            except WidgetAPIError as exc:
                if exc.retry_after is None or retries >= MAX_RETRIES:
                    raise
                retries += 1
                # Через модуль, а не `from asyncio import sleep`: тесты подменяют сон.
                await asyncio.sleep(exc.retry_after)

    async def send_widget(
        self,
        chat_id: int,
        html: str,
        *,
        caption: str | None = None,
        width: int = 0,
        height: int = 0,
    ) -> dict:
        """`sendWidget`: тело ровно `chat_id/html/width/height` (+ `caption`).

        `allow_queries` и прочие флаги в v1 не отправляем: виджет ничего у бота
        не спрашивает, значит `widget_query` не приходит.
        """
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "html": html,
            "width": width,
            "height": height,
        }
        if caption is not None:
            body["caption"] = caption
        result = await self.call("sendWidget", **body)
        return result if isinstance(result, dict) else {}

    async def answer_inline(
        self,
        query_id: str,
        results: list[dict],
        *,
        cache_time: int = 0,
        is_personal: bool = True,
    ) -> None:
        """`answerInlineQuery`: `results` уходят как есть, без pydantic-валидации."""
        await self.call(
            "answerInlineQuery",
            inline_query_id=query_id,
            results=results,
            cache_time=cache_time,
            is_personal=is_personal,
        )


_api: WidgetAPI | None = None


def configure(api: WidgetAPI) -> None:
    """Запоминает клиент для хендлеров (вызывается на старте приложения)."""
    global _api
    _api = api


def get_api() -> WidgetAPI:
    """Возвращает настроенный клиент; без `configure` — понятный `RuntimeError`."""
    if _api is None:
        raise RuntimeError(
            "Bot API-клиент не настроен: вызовите bot.compare.api.configure(api) "
            "при старте приложения."
        )
    return _api


__all__ = [
    "MAX_RETRIES",
    "Transport",
    "WidgetAPI",
    "WidgetAPIError",
    "configure",
    "get_api",
]
