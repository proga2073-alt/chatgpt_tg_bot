"""Инструменты, которые модель может вызывать (function calling): изображения, файлы, команды cmd."""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

from bot.utils import btn, kb
from config import settings
from db import repo
from db.session import SessionLocal
from llm.client import LLMError, edit_image, generate_image
from services import workspace

log = logging.getLogger(__name__)

MAX_WRITE_BYTES = 2_000_000
MAX_IMAGE_BYTES = 20_000_000
READ_LIMIT = 20_000
SHELL_CONFIRM_TIMEOUT = 120
SHELL_OUTPUT_LIMIT = 6000

TOOL_STATUS = {
    "generate_image": "🎨 Рисую изображение…",
    "edit_image": "🖌 Редактирую изображение…",
    "write_file": "📄 Создаю файл…",
    "read_file": "📖 Читаю файл…",
    "list_files": "📂 Смотрю файлы…",
    "run_command": "🖥 Жду подтверждения команды…",
}

TOOLS_PROMPT = (
    "У тебя есть инструменты. Используй их, когда пользователь просит картинку или файл:\n"
    "- generate_image — нарисовать изображение (промт лучше писать подробно, на английском);\n"
    "- edit_image — изменить загруженное или созданное ранее изображение (source — имя файла);\n"
    "- write_file — создать или перезаписать текстовый файл (код, документ, CSV) и отправить его пользователю;\n"
    "- read_file / list_files — прочитать файл пользователя или посмотреть список файлов.\n"
    "Чтобы отредактировать файл: read_file, затем write_file с полным новым содержимым. "
    "Не пересказывай содержимое созданного файла в ответе — коротко скажи, что сделано."
)
SHELL_PROMPT = (
    "\n- run_command — выполнить команду cmd на сервере (пользователь подтверждает каждую команду кнопкой). "
    "Рабочая папка — файлы пользователя. Команды должны быть безопасными и короткими; "
    "после выполнения кратко объясни результат."
)


@dataclass
class ToolContext:
    bot: Bot
    chat_id: int
    user_id: int
    is_admin: bool
    made: list[str] = field(default_factory=list)  # имена созданных файлов/изображений
    need_topup: bool = False  # не хватило ⭐ на изображение


def _fn(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def shell_available(is_admin: bool) -> bool:
    return is_admin and settings.enable_shell


def tool_specs(is_admin: bool) -> list[dict[str, Any]]:
    if not settings.tools_enabled:
        return []
    specs = [
        _fn("generate_image", "Сгенерировать изображение по описанию и отправить пользователю.", {
            "prompt": {"type": "string", "description": "Подробное описание изображения"},
            "aspect": {"type": "string", "enum": ["square", "landscape", "portrait"], "description": "Формат кадра"},
        }, ["prompt"]),
        _fn("edit_image", "Отредактировать существующее изображение по инструкции и отправить результат.", {
            "prompt": {"type": "string", "description": "Что изменить"},
            "source": {"type": "string", "description": "Имя файла изображения из рабочей папки"},
        }, ["prompt", "source"]),
        _fn("write_file", "Создать (или перезаписать) текстовый файл и отправить его пользователю.", {
            "filename": {"type": "string", "description": "Имя файла с расширением, например report.md"},
            "content": {"type": "string", "description": "Полное содержимое файла"},
        }, ["filename", "content"]),
        _fn("read_file", "Прочитать текстовый файл из рабочей папки пользователя.", {
            "filename": {"type": "string"},
            "offset": {"type": "integer", "description": "С какого символа читать (по умолчанию 0)"},
        }, ["filename"]),
        _fn("list_files", "Показать файлы в рабочей папке пользователя.", {}, []),
    ]
    if shell_available(is_admin):
        specs.append(_fn("run_command", "Выполнить команду cmd (пользователь подтверждает кнопкой).", {
            "command": {"type": "string", "description": "Команда для cmd"},
        }, ["command"]))
    return specs


def system_prompt_extra(is_admin: bool) -> str:
    return TOOLS_PROMPT + (SHELL_PROMPT if shell_available(is_admin) else "")


# ───────────────────────── исполнение ─────────────────────────


async def execute(ctx: ToolContext, name: str, raw_args: str) -> str:
    """Выполняет инструмент и возвращает текст-результат для модели. Исключения наружу не выходят."""
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return "Ошибка: аргументы не являются корректным JSON."
    if not isinstance(args, dict):
        return "Ошибка: ожидался JSON-объект с аргументами."
    handler = _HANDLERS.get(name)
    if handler is None or (name == "run_command" and not shell_available(ctx.is_admin)):
        return f"Ошибка: инструмент «{name}» недоступен."
    try:
        return await handler(ctx, args)
    except LLMError as exc:
        return f"Ошибка: {exc.user_message}"
    except Exception:  # noqa: BLE001
        log.exception("Tool %s failed", name)
        return "Ошибка при выполнении инструмента."


async def _charge_image(ctx: ToolContext) -> str | None:
    cost = 0 if ctx.is_admin else settings.image_cost_credits
    if not cost:
        return None
    async with SessionLocal() as s:
        ok = await repo.charge(s, ctx.user_id, cost)
        await s.commit()
    if ok:
        return None
    ctx.need_topup = True
    return "Ошибка: на балансе недостаточно ⭐ для изображения. Скажи пользователю, что нужно пополнить баланс (/buy)."


async def _refund_image(ctx: ToolContext) -> None:
    if ctx.is_admin or not settings.image_cost_credits:
        return
    async with SessionLocal() as s:
        await repo.add_credits(s, ctx.user_id, settings.image_cost_credits)
        await s.commit()


async def _deliver_image(ctx: ToolContext, data: bytes, caption: str) -> str:
    name = workspace.unique_name("img", ".png")
    workspace.save(ctx.user_id, name, data)
    file = BufferedInputFile(data, filename=name)
    if len(data) <= 9_500_000:
        await ctx.bot.send_photo(ctx.chat_id, file, caption=caption[:900] or None)
    else:
        await ctx.bot.send_document(ctx.chat_id, file, caption=caption[:900] or None)
    ctx.made.append(name)
    return name


async def _generate_image(ctx: ToolContext, a: dict[str, Any]) -> str:
    prompt = str(a.get("prompt", "")).strip()[:4000]
    if not prompt:
        return "Ошибка: пустое описание изображения."
    if (err := await _charge_image(ctx)) is not None:
        return err
    try:
        data = await generate_image(prompt, str(a.get("aspect", "square")))
    except BaseException:
        await _refund_image(ctx)
        raise
    name = await _deliver_image(ctx, data, "🎨 " + prompt[:200])
    return f"Изображение создано и отправлено пользователю. Файл в рабочей папке: {name} (его можно править через edit_image)."


async def _edit_image(ctx: ToolContext, a: dict[str, Any]) -> str:
    prompt = str(a.get("prompt", "")).strip()[:4000]
    path = workspace.resolve(ctx.user_id, str(a.get("source", "")))
    if not prompt:
        return "Ошибка: не указано, что изменить."
    if path is None:
        names = ", ".join(n for n, _ in workspace.list_files(ctx.user_id)[:15]) or "файлов нет"
        return f"Ошибка: файл не найден. Доступные файлы: {names}."
    if path.stat().st_size > MAX_IMAGE_BYTES:
        return "Ошибка: изображение слишком большое (максимум 20 МБ)."
    if (err := await _charge_image(ctx)) is not None:
        return err
    try:
        data = await edit_image(path.read_bytes(), path.name, prompt)
    except BaseException:
        await _refund_image(ctx)
        raise
    name = await _deliver_image(ctx, data, "🖌 " + prompt[:200])
    return f"Изображение отредактировано и отправлено пользователю. Новый файл: {name}."


async def _write_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    filename = workspace.safe_name(str(a.get("filename", "")))
    content = str(a.get("content", ""))
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return "Ошибка: файл слишком большой (максимум 2 МБ)."
    path = workspace.save(ctx.user_id, filename, data)
    await ctx.bot.send_document(ctx.chat_id, BufferedInputFile(data, filename=path.name), caption=f"📄 {path.name}")
    ctx.made.append(path.name)
    return f"Файл «{path.name}» ({len(data)} байт) сохранён и отправлен пользователю."


async def _read_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    path = workspace.resolve(ctx.user_id, str(a.get("filename", "")))
    if path is None:
        return "Ошибка: файл не найден. Вызови list_files, чтобы увидеть доступные."
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        return f"Это бинарный файл ({len(raw)} байт), прочитать как текст нельзя."
    text = raw.decode("utf-8", errors="replace")
    try:
        offset = max(0, int(a.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    chunk = text[offset : offset + READ_LIMIT]
    tail = f"\n[... показано {offset}–{offset + len(chunk)} из {len(text)} символов; продолжи с offset={offset + len(chunk)}]" if offset + READ_LIMIT < len(text) else ""
    return chunk + tail


async def _list_files(ctx: ToolContext, a: dict[str, Any]) -> str:
    files = workspace.list_files(ctx.user_id)
    return "\n".join(f"{n} — {size} байт" for n, size in files) if files else "Рабочая папка пуста."


# ───────────────────────── cmd (только админы, с подтверждением) ─────────────────────────

_pending: dict[str, tuple[int, asyncio.Future[bool]]] = {}


def resolve_shell(token: str, user_id: int, approved: bool) -> bool:
    """Вызывается из callback-хэндлера. True — запрос найден и принадлежит этому пользователю."""
    item = _pending.get(token)
    if item is None or item[0] != user_id:
        return False
    if not item[1].done():
        item[1].set_result(approved)
    return True


def _decode(data: bytes) -> str:
    encodings = ["utf-8", "cp866", "cp1251"] if os.name == "nt" else ["utf-8"]
    for enc in encodings:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


async def _exec(command: str, cwd: Path) -> tuple[int | None, str]:
    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.shell_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.communicate()
        return None, f"Команда прервана по таймауту ({settings.shell_timeout} с)."
    except asyncio.CancelledError:  # нажали «Стоп»
        proc.kill()
        raise
    text = _decode(out).strip()
    if len(text) > SHELL_OUTPUT_LIMIT:
        half = SHELL_OUTPUT_LIMIT // 2
        text = text[:half] + "\n[... вывод сокращён ...]\n" + text[-half:]
    return proc.returncode, text


async def _run_command(ctx: ToolContext, a: dict[str, Any]) -> str:
    command = str(a.get("command", "")).strip()
    if not command or len(command) > 2000:
        return "Ошибка: пустая или слишком длинная команда."
    cwd = workspace.user_dir(ctx.user_id)
    token = secrets.token_hex(6)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    _pending[token] = (ctx.user_id, future)
    ask = await ctx.bot.send_message(
        ctx.chat_id,
        "🖥 <b>Модель просит выполнить команду</b>\n"
        f"<pre>{html.escape(command)}</pre>\n"
        f"Папка: <code>{html.escape(str(cwd))}</code>\n"
        "Команда выполнится на сервере бота от имени запущенного процесса. Разрешить?",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([btn("✅ Выполнить", f"sh:ok:{token}"), btn("❌ Отклонить", f"sh:no:{token}")]),
    )
    try:
        approved = await asyncio.wait_for(future, timeout=SHELL_CONFIRM_TIMEOUT)
    except asyncio.TimeoutError:
        approved = False
    finally:
        _pending.pop(token, None)
        with contextlib.suppress(Exception):
            await ask.edit_reply_markup(reply_markup=None)
    if not approved:
        log.info("Shell command rejected by user %s: %r", ctx.user_id, command)
        return "Пользователь отклонил выполнение команды (или не ответил вовремя)."

    log.warning("Shell command approved by user %s: %r", ctx.user_id, command)
    code, output = await _exec(command, cwd)
    shown = output or "(пустой вывод)"
    with contextlib.suppress(Exception):
        await ctx.bot.send_message(
            ctx.chat_id, f"📟 Код возврата: <code>{code}</code>\n<pre>{html.escape(shown[:3500])}</pre>", parse_mode=ParseMode.HTML
        )
    return f"Код возврата: {code}\n{shown}"


_HANDLERS = {
    "generate_image": _generate_image,
    "edit_image": _edit_image,
    "write_file": _write_file,
    "read_file": _read_file,
    "list_files": _list_files,
    "run_command": _run_command,
}
