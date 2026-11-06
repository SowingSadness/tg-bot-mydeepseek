"""Общие настройки pytest: фиктивное окружение, временные каталоги и `sys.path`.

Важно: переменные окружения выставляются **до** первого импорта `bot.*`, потому что
`bot/config.py` создаёт `settings = Settings()` на импорте модуля и падает без
`TELEGRAM_BOT_TOKEN` / `DEEPSEEK_API_KEY` / `BOT_USERNAME`.

Временные каталоги создаются в одном месте (`_TMP_ROOT`) и удаляются после прогона:
в некоторых окружениях `tempfile.gettempdir()` указывает на текущий каталог, и без
уборки `pytest-of-*` и `cmp-cache-*` остаются в корне репозитория.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Корень репозитория в sys.path: тесты запускаются и как `pytest`, и как `python -m pytest`.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Фиктивные значения: настоящие токены в тестах не нужны, сеть не используется.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test")
os.environ.setdefault("BOT_USERNAME", "test_bot")

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="pytest-cmp-"))
# Кеш сравнений — во временном каталоге этого прогона.
os.environ.setdefault("CMP_CACHE_DIR", str(_TMP_ROOT / "cmp-cache"))


def pytest_configure(config) -> None:  # noqa: ANN001
    """Держит и `tmp_path`-каталоги pytest внутри `_TMP_ROOT`, чтобы не мусорить в репозитории."""
    if not getattr(config.option, "basetemp", None):
        config.option.basetemp = str(_TMP_ROOT / "basetemp")


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001, ANN001
    """Удаляет временные каталоги после прогона."""
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
