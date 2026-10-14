"""Асинхронный клиент DeepSeek API (OpenAI-совместимый)."""

from __future__ import annotations

import logging
from typing import AsyncIterator, Sequence

from openai import NOT_GIVEN, APIError, APIStatusError, APITimeoutError, AsyncOpenAI

from .config import settings

log = logging.getLogger(__name__)


class DeepSeekError(Exception):
    """Понятная пользователю ошибка обращения к DeepSeek."""


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 180.0,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._model = model

    def _build_payload(
        self,
        text: str,
        images: Sequence[str] | None,
        thinking: bool,
        max_tokens: int | None,
        system: str | None,
    ) -> dict:
        content: str | list[dict]
        if images:
            blocks: list[dict] = []
            if text.strip():
                blocks.append({"type": "text", "text": text})
            for url in images:
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            content = blocks
        else:
            content = text

        extra_body: dict = {"thinking": {"type": "enabled" if thinking else "disabled"}}
        if thinking:
            extra_body["reasoning_effort"] = "high"

        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        return {
            "model": self._model,
            "messages": messages,
            "extra_body": extra_body,
            # Явный None SDK отправляет как "max_tokens": null, поэтому NOT_GIVEN.
            "max_tokens": max_tokens if max_tokens is not None else NOT_GIVEN,
        }

    async def ask(
        self,
        text: str,
        images: Sequence[str] | None = None,
        thinking: bool = False,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> str:
        """Отправляет запрос и возвращает текст ответа целиком.

        ``images`` — список ссылок: data-URL (base64) или публичных http(s)-адресов.
        ``max_tokens`` ограничивает длину ответа (ускоряет генерацию).
        ``system`` — необязательный системный промпт.
        """
        payload = self._build_payload(text, images, thinking, max_tokens, system)
        try:
            response = await self._client.chat.completions.create(**payload)
        except APITimeoutError as exc:
            raise DeepSeekError("DeepSeek не ответил вовремя. Попробуйте ещё раз.") from exc
        except APIStatusError as exc:
            raise DeepSeekError(self._describe(exc)) from exc
        except APIError as exc:
            log.exception("Ошибка DeepSeek API")
            raise DeepSeekError(f"Ошибка DeepSeek API: {exc}") from exc

        answer = (response.choices[0].message.content or "").strip()
        if not answer:
            raise DeepSeekError("Модель вернула пустой ответ. Переформулируйте запрос.")
        return answer

    async def stream(
        self,
        text: str,
        images: Sequence[str] | None = None,
        thinking: bool = False,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        """Отдаёт куски ответа по мере генерации.

        Нужен там, где важен первый байт: Telegram аннулирует inline-запрос
        примерно через 10 секунд, поэтому частичный ответ лучше, чем ничего.
        """
        payload = self._build_payload(text, images, thinking, max_tokens, system)
        try:
            stream = await self._client.chat.completions.create(**payload, stream=True)
        except APITimeoutError as exc:
            raise DeepSeekError("DeepSeek не ответил вовремя. Попробуйте ещё раз.") from exc
        except APIStatusError as exc:
            raise DeepSeekError(self._describe(exc)) from exc
        except APIError as exc:
            raise DeepSeekError(f"Ошибка DeepSeek API: {exc}") from exc

        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                piece = chunk.choices[0].delta.content
                if piece:
                    yield piece
        except APIError as exc:
            raise DeepSeekError(f"Ошибка DeepSeek API: {exc}") from exc
        finally:
            try:
                await stream.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _describe(exc: APIStatusError) -> str:
        status = exc.status_code
        if status == 400:
            return "DeepSeek отклонил запрос (400). Проверьте текст или формат картинки."
        if status == 401:
            return "Неверный DEEPSEEK_API_KEY. Проверьте ключ в .env."
        if status == 402:
            return "Недостаточно средств на балансе DeepSeek. Пополните счёт."
        if status == 429:
            return "Слишком много запросов к DeepSeek. Подождите немного."
        if status >= 500:
            return "DeepSeek временно недоступен. Повторите попытку позже."
        return f"DeepSeek API вернул ошибку {status}."


deepseek = DeepSeekClient(
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_base_url,
    model=settings.deepseek_model,
    timeout=settings.request_timeout,
)
