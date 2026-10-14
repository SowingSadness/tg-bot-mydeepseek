"""Хранилище состояния в памяти процесса.

Контекст диалога не сохраняется: бот отвечает на каждый запрос независимо.
Здесь хранится только персональная настройка режима рассуждений.
"""

from __future__ import annotations

_thinking_users: set[int] = set()


def is_thinking(user_id: int) -> bool:
    return user_id in _thinking_users


def toggle_thinking(user_id: int) -> bool:
    """Переключает режим рассуждений и возвращает новое состояние."""
    if user_id in _thinking_users:
        _thinking_users.discard(user_id)
        return False
    _thinking_users.add(user_id)
    return True


def reset_user(user_id: int) -> None:
    _thinking_users.discard(user_id)
