"""Inline-клавиатуры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .state import is_thinking


def think_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    label = "🧠 Рассуждения: вкл" if is_thinking(user_id) else "⚡ Рассуждения: выкл"
    builder.button(text=label, callback_data="toggle_think")
    return builder.as_markup()
