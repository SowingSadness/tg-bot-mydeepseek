"""Настройки шага 1: поля `Settings`, шаблон бабла и `.env.example` не разъезжаются."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from bot.config import Settings, settings

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "bot" / "compare" / "template.html"
ENV_EXAMPLE = ROOT / ".env.example"

# Ожидаемые дефолты всех новых полей (кроме cmp_cache_dir: он переопределяется окружением).
EXPECTED_DEFAULTS: dict[str, object] = {
    "widget_api_base": "https://api.telegram.org/beta",
    "widget_api_test_env": False,
    "cmp_cache_ttl_days": 30,
    "cmp_cache_max_entries": 2000,
    "cmp_target_width": 430,
    "cmp_min_width": 160,
    "cmp_start_quality": 34,
    "cmp_min_quality": 12,
    "cmp_zlib_margin": 64,
    "cmp_max_bytes": 8388608,
    "cmp_album_wait": 0.9,
    "cmp_aspect": "3:2",
    "cmp_id_digits": 6,
    "cmp_share_by_id": False,
    "cmp_show_hint": True,
    "cmp_allow_private_hosts": False,
    "cmp_labels": "До|После",
    "cmp_caption": "Номер сравнения:\n@{bot} #{id}",
    "cmp_title": "Сравнение",
    "cmp_hint": "Проведите ↔",
    "cmp_webapp_short_name": "",
    "cmp_preview_width": 2560,
    # Шаг 7: мини-апп /app/cmp, оригиналы и срок жизни initData.
    "cmp_webapp_port": 0,
    "cmp_webapp_host": "127.0.0.1",
    "cmp_webapp_public_url": "",
    "cmp_originals_width": 1600,
    "cmp_webapp_auth_ttl": 86400,
}

NEW_FIELDS = ("cmp_cache_dir", *EXPECTED_DEFAULTS)
PLACEHOLDERS = ("{{TITLE}}", "{{AR}}", "{{LABEL_A}}", "{{LABEL_B}}", "{{HINT}}", "{{A}}", "{{B}}")
ENV_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _env_example_keys() -> set[str]:
    return set(ENV_KEY_RE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def test_all_new_fields_declared() -> None:
    assert len(NEW_FIELDS) == 28
    missing = [name for name in NEW_FIELDS if name not in Settings.model_fields]
    assert not missing, f"в Settings нет полей: {missing}"


def test_new_fields_defaults() -> None:
    for name, expected in EXPECTED_DEFAULTS.items():
        assert Settings.model_fields[name].default == expected, name
    # Явно: 12 — последняя ступень подгонки качества; на тяжёлых фото ниже уже мыло.
    assert Settings.model_fields["cmp_min_quality"].default == 12
    assert settings.cmp_min_quality == 12


def test_cache_dir_default_and_env() -> None:
    assert Settings.model_fields["cmp_cache_dir"].default == Path("var/cmp")
    assert isinstance(settings.cmp_cache_dir, Path)
    assert settings.cmp_cache_dir == Path(os.environ["CMP_CACHE_DIR"])


def test_log_level_defaults_to_error(monkeypatch) -> None:
    """Требование пользователя: в проде тишина, только ERROR."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert Settings.model_fields["log_level"].default == "ERROR"
    assert _settings().log_level == "ERROR"


def test_log_level_env_override(monkeypatch) -> None:
    """Docker/`.env` переопределяют уровень без изменения кода."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert _settings().log_level == "DEBUG"


def test_labels_pair() -> None:
    explicit = Settings(
        telegram_bot_token="123:test",
        deepseek_api_key="sk-test",
        bot_username="test_bot",
        cmp_labels="До|После",
    )
    assert explicit.cmp_labels_pair == ("До", "После")
    assert settings.cmp_labels_pair == ("До", "После")


def test_labels_pair_rejects_bad_value() -> None:
    with pytest.raises(ValueError):
        Settings(
            telegram_bot_token="123:test",
            deepseek_api_key="sk-test",
            bot_username="test_bot",
            cmp_labels="До|После|Ещё",
        )


def _settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": "123:test",
        "deepseek_api_key": "sk-test",
        "bot_username": "test_bot",
    }
    values.update(overrides)
    return Settings(**values)


def test_default_aspect_pair() -> None:
    assert _settings().cmp_aspect_pair == (3, 2)
    assert settings.cmp_aspect_pair == (3, 2)
    assert settings.cmp_aspect == "3:2"


def test_aspect_pair_accepts_common_and_wide_ratios() -> None:
    assert _settings(cmp_aspect="4:3").cmp_aspect_pair == (4, 3)
    assert _settings(cmp_aspect="16:9").cmp_aspect_pair == (16, 9)
    # Дробные пропорции сводятся к целой паре без потери смысла.
    assert _settings(cmp_aspect="2.66:1").cmp_aspect_pair == (133, 50)


def test_aspect_rejects_bad_values() -> None:
    for value in ("", "3", "3:0", "0:2", "-3:2", "3:x", "a:b", "3:2:1"):
        with pytest.raises(ValueError):
            _settings(cmp_aspect=value)


def test_pending_ttl_is_gone() -> None:
    assert "cmp_" "pending_ttl" not in Settings.model_fields
    assert "CMP_" "PENDING_TTL" not in _env_example_keys()


def test_template_placeholders() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    for placeholder in PLACEHOLDERS:
        assert placeholder in html, placeholder
    # Шторка должна разрешать вертикальный скролл чата.
    assert "touch-action: pan-y" in html


def test_caption_default_is_two_lines() -> None:
    """Подпись по умолчанию: заголовок и `@бот #номер` отдельной строкой."""
    caption = _settings().cmp_caption
    assert caption == "Номер сравнения:\n@{bot} #{id}"
    assert caption.splitlines() == ["Номер сравнения:", "@{bot} #{id}"]
    assert caption.format(id="482913", bot="test_bot") == (
        "Номер сравнения:\n@test_bot #482913"
    )


def test_env_example_caption_keeps_hash_and_newline(tmp_path) -> None:
    """`CMP_CAPTION` в двойных кавычках: dotenv не режет по `#` и раскрывает `\\n`."""
    line = 'CMP_CAPTION="Номер сравнения:\\n@{bot} #{id}"'
    assert line in ENV_EXAMPLE.read_text(encoding="utf-8")

    env_file = tmp_path / ".env"
    env_file.write_text(line + "\n", encoding="utf-8")
    loaded = Settings(
        _env_file=env_file,
        telegram_bot_token="123:test",
        deepseek_api_key="sk-test",
        bot_username="test_bot",
    )
    assert loaded.cmp_caption == "Номер сравнения:\n@{bot} #{id}"
    assert loaded.cmp_caption.splitlines() == ["Номер сравнения:", "@{bot} #{id}"]
    assert loaded.cmp_caption.format(id="482913", bot="test_bot") == (
        "Номер сравнения:\n@test_bot #482913"
    )


def test_env_example_has_all_new_keys() -> None:
    keys = _env_example_keys()
    missing = sorted({name.upper() for name in NEW_FIELDS} - keys)
    assert not missing, f"в .env.example нет: {missing}"
    # Переменные альбомного флоу и пропорций бабла документированы явно.
    assert {"CMP_ALBUM_WAIT", "CMP_ASPECT"} <= keys
    # Мини-апп: по умолчанию выключен, но порт/хост/адрес/оригиналы/TTL есть в шаблоне.
    assert {
        "CMP_WEBAPP_PORT",
        "CMP_WEBAPP_HOST",
        "CMP_WEBAPP_PUBLIC_URL",
        "CMP_ORIGINALS_WIDTH",
        "CMP_WEBAPP_AUTH_TTL",
    } <= keys


def test_webapp_fields_defaults_and_override() -> None:
    """Мини-апп выключен по умолчанию и настраивается через окружение/аргументы."""
    assert _settings().cmp_webapp_port == 0
    assert _settings().cmp_webapp_host == "127.0.0.1"
    assert _settings().cmp_webapp_public_url == ""
    assert _settings().cmp_originals_width == 1600
    assert _settings().cmp_webapp_auth_ttl == 86400

    tuned = _settings(
        cmp_webapp_port=8081,
        cmp_webapp_host="0.0.0.0",
        cmp_webapp_public_url="https://example.com/app/cmp",
        cmp_originals_width=2000,
        cmp_webapp_auth_ttl=3600,
    )
    assert tuned.cmp_webapp_port == 8081
    assert tuned.cmp_webapp_host == "0.0.0.0"
    assert tuned.cmp_webapp_public_url == "https://example.com/app/cmp"
    assert tuned.cmp_originals_width == 2000
    assert tuned.cmp_webapp_auth_ttl == 3600


def test_env_example_log_level_is_error() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "LOG_LEVEL=ERROR" in text
    assert "DEBUG" in text  # комментарий о том, как включить отладку


def test_env_example_and_settings_keys_match() -> None:
    env_keys = {key for key in _env_example_keys() if key.startswith(("CMP_", "WIDGET_"))}
    field_keys = {
        name.upper()
        for name in Settings.model_fields
        if name.startswith(("cmp_", "widget_"))
    }
    assert env_keys == field_keys, (
        f"лишние в .env.example: {sorted(env_keys - field_keys)}; "
        f"нет в .env.example: {sorted(field_keys - env_keys)}"
    )
