"""Markdown от модели → HTML для Telegram (parse_mode=HTML) с безопасным экранированием.

Поддержано: блоки кода, `inline`, **жирный**, *курсив*, [ссылки](https://…), # заголовки, списки.
Всё остальное экранируется. Если Telegram всё же отвергнет разметку, вызывающий код
откатывается на plain-текст (см. bot/handlers.py).
"""
from __future__ import annotations

import html
import re

_FENCE = re.compile(r"```(?:([\w+#.-]+)[ \t]*\n|\n)?(.*?)(?:```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HEADER = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")

TG_LIMIT = 4096


def _inline(text: str) -> str:
    text = text.replace("\x00", "")
    stash: list[str] = []

    def keep_code(m: re.Match[str]) -> str:
        stash.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(stash) - 1}\x00"

    text = _INLINE_CODE.sub(keep_code, text)
    text = html.escape(text, quote=False)
    text = _HEADER.sub(r"<b>\1</b>", text)
    text = _BULLET.sub(lambda m: m.group(1) + "• ", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', text)
    return _PLACEHOLDER.sub(lambda m: stash[int(m.group(1))], text)


def _code_block(lang: str | None, code: str) -> str:
    code = code.strip("\n")
    if not code:
        return ""
    body = html.escape(code, quote=False)
    lang = re.sub(r"[^\w+#.-]", "", lang or "")
    if lang:
        return f'<pre><code class="language-{lang}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def to_html(text: str) -> str:
    """Конвертирует Markdown в Telegram-HTML. Незакрытый ``` (стриминг) закрывается автоматически."""
    out: list[str] = []
    pos = 0
    for m in _FENCE.finditer(text):
        out.append(_inline(text[pos : m.start()]))
        out.append(_code_block(m.group(1), m.group(2)))
        pos = m.end()
    out.append(_inline(text[pos:]))
    return "".join(out).strip()


def _take(text: str, size: int) -> str:
    if len(text) <= size:
        return text
    cut = text.rfind("\n", 0, size)
    if cut < size // 2:
        cut = size
    return text[:cut]


def render_chunks(text: str, limit: int = TG_LIMIT, start: int = 3500) -> list[tuple[str, str]]:
    """Режет ответ на сообщения ≤ limit символов уже *после* конвертации в HTML.

    Возвращает список (html, plain). Блок кода, разорванный границей, переоткрывается в следующей части.
    """
    rest = text.strip() or "…"
    chunks: list[tuple[str, str]] = []
    while rest:
        size = start
        while True:
            piece = _take(rest, size)
            rendered = to_html(piece)
            if len(rendered) <= limit or size <= 256:
                break
            size //= 2
        rest_after = rest[len(piece) :].lstrip("\n")
        if piece.count("```") % 2 and rest_after:
            rest_after = "```\n" + rest_after
        chunks.append((rendered or html.escape(piece), piece))
        rest = rest_after
    return chunks
