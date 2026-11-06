"""Конфигурация бота, читается из переменных окружения / файла .env."""

from __future__ import annotations

import re
from fractions import Fraction
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
    # DEBUG — логи Telegram API и входящих сообщений; в проде ERROR (по умолчанию).
    log_level: str = "ERROR"

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

    # Сравнение картинок (/cmp): дисковый кеш, пропорции бабла и лимиты виджета.
    cmp_cache_dir: Path = Path("var/cmp")
    cmp_cache_ttl_days: int = 30
    cmp_cache_max_entries: int = 2000
    cmp_target_width: int = 430
    cmp_min_width: int = 160
    cmp_start_quality: int = 34
    # Нижняя граница: на тяжёлых фото 430 px влезает только при q12.
    cmp_min_quality: int = 12
    # Запас под лимит zlib(level 9) = 4096 байт.
    cmp_zlib_margin: int = 64
    cmp_max_bytes: int = 8388608
    # Сколько ждать остальные картинки альбома после подписи /cmp (секунды).
    cmp_album_wait: float = 0.9
    # Пропорции бабла: A:B, например 3:2 (по умолчанию), 4:3, 16:9, 2.66:1.
    cmp_aspect: str = "3:2"
    cmp_id_digits: int = 6
    cmp_share_by_id: bool = False
    cmp_show_hint: bool = True
    cmp_allow_private_hosts: bool = False
    cmp_labels: str = "До|После"
    cmp_caption: str = "Сравнение #{id} · {a} / {b}"
    cmp_title: str = "Сравнение"
    cmp_hint: str = "Проведите ↔"
    # Short name мини-аппа из BotFather для кнопки «Открыть сравнение».
    # Пусто = кнопка не добавляется (мини-апп ещё не настроен).
    cmp_webapp_short_name: str = ""
    # Ширина композита-превью «A|B» в пикселях (Telegram ужмёт по длинной стороне).
    cmp_preview_width: int = 2560
    # Отдавать ли в inline-выдаче саму шторку (widget-контент).
    # Бета Bot API отклоняет widget в inline (INPUT_MEDIA_WIDGET_NOT_ALLOWED),
    # поэтому по умолчанию false: остаются фото-превью, кнопка и текстовый результат.
    cmp_inline_widget: bool = False

    @field_validator("cmp_labels")
    @classmethod
    def _check_labels(cls, value: str) -> str:
        """Подписи задаются парой через `|`: `До|После`."""
        if len(value.split("|")) != 2:
            raise ValueError("CMP_LABELS должен содержать ровно две подписи через '|'")
        return value

    @field_validator("cmp_aspect")
    @classmethod
    def _check_aspect(cls, value: str) -> str:
        """Пропорции задаются как `A:B`, оба числа положительные."""
        _aspect_pair(value)  # валидация без побочного эффекта
        return value.strip()

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
    def cmp_aspect_pair(self) -> tuple[int, int]:
        """Пропорции бабла целыми числами: `3:2` → `(3, 2)`, `2.66:1` → `(133, 50)`."""
        return _aspect_pair(self.cmp_aspect)

    @property
    def allowed_ids(self) -> set[int]:
        ids: set[int] = set()
        for chunk in self.allowed_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
        return ids


_ASPECT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$")


def _aspect_pair(value: str) -> tuple[int, int]:
    """Разбирает `A:B` в целую пару; дроби вроде `2.66:1` сводятся к `133:50`."""
    match = _ASPECT_RE.fullmatch(str(value))
    if match is None:
        raise ValueError("CMP_ASPECT должен быть в формате A:B (например 3:2)")
    left, right = (Fraction(part) for part in match.groups())
    if left <= 0 or right <= 0:
        raise ValueError("CMP_ASPECT: оба числа должны быть больше нуля")
    ratio = (left / right).limit_denominator(1000)
    return ratio.numerator, ratio.denominator


settings = Settings()
