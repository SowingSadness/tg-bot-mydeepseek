"""Утилиты: работа с Markdown, ссылками на картинки и длиной сообщений."""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096

_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s<>\"')\]]+?\.(?:jpg|jpeg|png|gif|webp)(?:\?[^\s<>\"')\]]*)?",
    re.IGNORECASE,
)

_CODE_BLOCK_RE = re.compile(r"```[ \t]*[\w+-]*[ \t]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_UNDERSCORE_BOLD_RE = re.compile(r"__(.+?)__")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+)$", re.MULTILINE)


def extract_image_urls(text: str, limit: int = 10) -> list[str]:
    """Возвращает уникальные ссылки на изображения в порядке появления."""
    urls: list[str] = []

    def add(url: str) -> None:
        if url not in urls:
            urls.append(url)

    for match in _MD_IMAGE_RE.finditer(text):
        add(match.group(1))
    for match in _IMAGE_URL_RE.finditer(text):
        add(match.group(0))
    return urls[:limit]


def markdown_to_html(text: str) -> str:
    """Грубое, но безопасное преобразование Markdown в HTML для Telegram."""
    stash: list[str] = []

    def keep(value: str) -> str:
        stash.append(value)
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(
        lambda m: keep(f"<pre>{html.escape(m.group(1).strip(chr(10)))}</pre>"), text
    )
    text = _INLINE_CODE_RE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _MD_IMAGE_RE.sub(
        lambda m: keep(f'<a href="{html.escape(m.group(1), quote=True)}">🖼 изображение</a>'), text
    )

    text = html.escape(text)

    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _UNDERSCORE_BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)

    for index, value in enumerate(stash):
        text = text.replace(f"\x00{index}\x00", value)
    return text


def telegram_html(text: str, limit: int = TELEGRAM_LIMIT) -> tuple[str, str | None]:
    """Готовит текст для Telegram: HTML, если он влезает в лимит, иначе чистый текст."""
    html_text = markdown_to_html(text)
    if len(html_text) <= limit:
        return html_text, "HTML"
    return text[:limit], None


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Разбивает текст на части, не превышающие лимит Telegram."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) <= limit:
            current += line
            continue
        if current:
            chunks.append(current.rstrip())
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line

    if current.strip():
        chunks.append(current.rstrip())
    return [chunk for chunk in chunks if chunk]
