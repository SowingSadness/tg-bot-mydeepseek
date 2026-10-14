"""Конфигурация бота, читается из переменных окружения / файла .env."""

from __future__ import annotations

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

    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    bot_username: str = "sowing_bot"
    request_timeout: float = 180.0
    log_level: str = "INFO"

    # Список ID через запятую. Пусто = доступ открыт всем.
    allowed_user_ids: str = ""

    @property
    def allowed_ids(self) -> set[int]:
        ids: set[int] = set()
        for chunk in self.allowed_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
        return ids


settings = Settings()
