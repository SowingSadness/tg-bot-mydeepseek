"""Конфигурация бота, читается из переменных окружения / файла .env."""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str
    deepseek_api_key: str
    bot_username: str

    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    request_timeout: float = 180.0
    log_level: str = "INFO"

    # Лимит длины ответов в личке/группах. 0 = без ограничения.
    chat_max_tokens: int = 0

    # Настройки inline-режима (Telegram даёт на ответ ~10 секунд).
    inline_timeout: float = 8.5
    inline_max_tokens: int = 350
    inline_cache_ttl: int = 300
    warmup_on_start: bool = True

    # Прогрессивный ответ: показать частичный результат, не дожидаясь конца генерации.
    inline_early_update: bool = True
    inline_early_delay: float = 1.2
    inline_early_min_chars: int = 120

    # Список ID через запятую. Пусто = доступ открыт всем.
    allowed_user_ids: str = ""

    @field_validator("bot_username")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        """Принимает `@name` или `name`, возвращает `name`."""
        value = value.strip().lstrip("@").strip()
        if not value:
            raise ValueError("BOT_USERNAME не может быть пустым")
        return value

    @property
    def allowed_ids(self) -> set[int]:
        ids: set[int] = set()
        for chunk in self.allowed_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
        return ids


settings = Settings()
