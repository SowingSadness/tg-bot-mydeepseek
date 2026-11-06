"""Настройки шага 1: поля `Settings`, шаблон бабла и `.env.example` не разъезжаются."""

from __future__ import annotations

import os
import re
from pathlib import Path

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
    "cmp_min_quality": 16,
    "cmp_zlib_margin": 64,
    "cmp_max_bytes": 8388608,
    "cmp_pending_ttl": 300,
    "cmp_id_digits": 6,
    "cmp_share_by_id": False,
    "cmp_show_hint": True,
    "cmp_allow_private_hosts": False,
    "cmp_labels": "До|После",
    "cmp_caption": "Сравнение #{id} · {a} / {b}",
    "cmp_title": "Сравнение",
    "cmp_hint": "Проведите ↔",
}

NEW_FIELDS = ("cmp_cache_dir", *EXPECTED_DEFAULTS)
PLACEHOLDERS = ("{{TITLE}}", "{{AR}}", "{{LABEL_A}}", "{{LABEL_B}}", "{{HINT}}", "{{A}}", "{{B}}")
ENV_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _env_example_keys() -> set[str]:
    return set(ENV_KEY_RE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def test_all_new_fields_declared() -> None:
    assert len(NEW_FIELDS) == 20
    missing = [name for name in NEW_FIELDS if name not in Settings.model_fields]
    assert not missing, f"в Settings нет полей: {missing}"


def test_new_fields_defaults() -> None:
    for name, expected in EXPECTED_DEFAULTS.items():
        assert Settings.model_fields[name].default == expected, name


def test_cache_dir_default_and_env() -> None:
    assert Settings.model_fields["cmp_cache_dir"].default == Path("var/cmp")
    assert isinstance(settings.cmp_cache_dir, Path)
    assert settings.cmp_cache_dir == Path(os.environ["CMP_CACHE_DIR"])


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
    import pytest

    with pytest.raises(ValueError):
        Settings(
            telegram_bot_token="123:test",
            deepseek_api_key="sk-test",
            bot_username="test_bot",
            cmp_labels="До|После|Ещё",
        )


def test_template_placeholders() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    for placeholder in PLACEHOLDERS:
        assert placeholder in html, placeholder
    # Шторка должна разрешать вертикальный скролл чата.
    assert "touch-action: pan-y" in html


def test_env_example_has_all_new_keys() -> None:
    keys = _env_example_keys()
    expected = {name.upper() for name in NEW_FIELDS}
    missing = sorted(expected - keys)
    assert not missing, f"в .env.example нет: {missing}"


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
