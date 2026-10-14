"""Асинхронный клиент DeepSeek API (OpenAI-совместимый)."""

from __future__ import annotations

import logging
from typing import Sequence

from openai import APIError, APIStatusError, APITimeoutError, AsyncOpenAI

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

    async def ask(
        self,
        text: str,
        images: Sequence[str] | None = None,
        thinking: bool = False,
    ) -> str:
        """Отправляет запрос и возвращает текст ответа.

        ``images`` — список ссылок: data-URL (base64) или публичных http(s)-адресов.
        """
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

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": content}],
                extra_body=extra_body,
            )
        except APITimeoutError as exc:
            raise DeepSeekError("DeepSeek не ответил вовремя. Попробуйте ещё раз.") from exc
        except APIStatusError as exc:
            raise DeepSeekError(self._describe(exc)) from exc
        except APIError as exc:
            log.exception("Ошибка DeepSeek API")
            raise DeepSeekError(f"Ошибка DeepSeek API: {exc}") from exc

        message = response.choices[0].message
        answer = (message.content or "").strip()
        if not answer:
            raise DeepSeekError("Модель вернула пустой ответ. Переформулируйте запрос.")
        return answer

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
