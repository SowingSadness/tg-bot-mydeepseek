"""Конфигурация бота, читается из переменных окружения / файла .env."""

from __future__ import annotations

from pathlib import Path

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

    # Bot API 10.4: HTML-виджеты (баблы). Метод sendWidget пока живёт на бете,
    # поэтому база настраивается: после GA достаточно поставить https://api.telegram.org.
    widget_api_base: str = "https://api.telegram.org/beta"
    # true = методы зовутся как /bot<token>/test/<method> (тестовый контур Telegram).
    widget_api_test_env: bool = False

    # Сравнение картинок (/cmp): дисковый кеш, кроп 4:3 и лимиты виджета.
    cmp_cache_dir: Path = Path("var/cmp")
    cmp_cache_ttl_days: int = 30
    cmp_cache_max_entries: int = 2000
    cmp_target_width: int = 430
    cmp_min_width: int = 160
    cmp_start_quality: int = 34
    cmp_min_quality: int = 16
    # Запас под лимит zlib(level 9) = 4096 байт.
    cmp_zlib_margin: int = 64
    cmp_max_bytes: int = 8388608
    cmp_pending_ttl: int = 300
    cmp_id_digits: int = 6
    cmp_share_by_id: bool = False
    cmp_show_hint: bool = True
    cmp_allow_private_hosts: bool = False
    cmp_labels: str = "До|После"
    cmp_caption: str = "Сравнение #{id} · {a} / {b}"
    cmp_title: str = "Сравнение"
    cmp_hint: str = "Проведите ↔"

    @field_validator("cmp_labels")
    @classmethod
    def _check_labels(cls, value: str) -> str:
        """Подписи задаются парой через `|`: `До|После`."""
        if len(value.split("|")) != 2:
            raise ValueError("CMP_LABELS должен содержать ровно две подписи через '|'")
        return value

    @field_validator("bot_username")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        """Принимает `@name` или `name`, возвращает `name`."""
        value = value.strip().lstrip("@").strip()
        if not value:
            raise ValueError("BOT_USERNAME не может быть пустым")
        return value

    @property
    def cmp_labels_pair(self) -> tuple[str, str]:
        """Пара подписей бабла: (нижний слой, верхний слой)."""
        first, second = self.cmp_labels.split("|")
        return first.strip(), second.strip()

    @property
    def allowed_ids(self) -> set[int]:
        ids: set[int] = set()
        for chunk in self.allowed_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
        return ids


settings = Settings()
