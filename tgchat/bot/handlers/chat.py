"""Основной чат: стриминг ответа, инструменты модели (изображения, файлы, cmd), «Стоп» и «Заново»."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, Message

from bot import tools
from bot.keyboards import buy_kb
from bot.utils import EMPTY_KB, btn, edit, kb, send, with_flood_retry
from config import settings
from llm.client import LLMError, stream_chat
from services import chat, workspace
from services.tgmarkdown import render_chunks

log = logging.getLogger(__name__)
router = Router(name="chat")

STOP_KB = kb([btn("⏹ Стоп", "stop")])
MAX_USER_TEXT = 8000
INLINE_FILE_LIMIT = 20_000  # маленькие текстовые файлы подставляются в сообщение целиком


def _after_kb(need_topup: bool = False):
    rows = [[btn("🔄 Заново", "regen"), btn("🆕 Новый диалог", "m:new")]]
    if need_topup:
        rows.append([btn("⭐ Пополнить баланс", "m:bal")])
    return kb(*rows)


def _retry_kb():
    return kb([btn("🔄 Повторить", "regen"), btn("🆕 Новый диалог", "m:new")])


async def _typing_loop(bot: Bot, chat_id: int) -> None:
    while True:
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id, "typing")
        await asyncio.sleep(4.5)


def _compose(user_id: int, text: str) -> str:
    """Добавляет к сообщению сведения о недавно загруженных файлах (и содержимое небольших текстовых)."""
    names = workspace.pop_uploads(user_id)
    if not names:
        return text
    parts: list[str] = []
    for name in names:
        path = workspace.resolve(user_id, name)
        if path is None:
            continue
        size = path.stat().st_size
        note = f"[Пользователь загрузил файл: {name} ({size} байт)]"
        if size <= INLINE_FILE_LIMIT:
            raw = path.read_bytes()
            if b"\x00" not in raw[:4096]:
                note += f"\nСодержимое «{name}»:\n```\n{raw.decode('utf-8', errors='replace')}\n```"
        parts.append(note)
    return "\n\n".join([*parts, text])


def _with_tool_prompt(messages: list[dict[str, str]], specs: list[dict[str, Any]], is_admin: bool) -> list[dict[str, Any]]:
    if not specs:
        return list(messages)
    extra = tools.system_prompt_extra(is_admin)
    msgs = [dict(m) for m in messages]
    if msgs and msgs[0]["role"] == "system":
        msgs[0]["content"] += "\n\n" + extra
    else:
        msgs.insert(0, {"role": "system", "content": extra})
    return msgs


async def run_turn(bot: Bot, chat_id: int, user_id: int, username: str | None, text: str | None, *, regenerate: bool = False) -> None:
    try:
        await chat.admit(user_id)
    except chat.RequestRejected as exc:
        await bot.send_message(chat_id, exc.message)
        return

    stop = asyncio.Event()
    chat.cancel_events[user_id] = stop
    is_admin = user_id in settings.admin_set
    ctx = tools.ToolContext(bot=bot, chat_id=chat_id, user_id=user_id, is_admin=is_admin)
    specs = tools.tool_specs(is_admin)

    turn: chat.Turn | None = None
    placeholder: Message | None = None
    typing: asyncio.Task | None = None
    full = ""
    error: str | None = None
    payment_kb = None
    stopped = False
    try:
        if not regenerate and text is not None:
            text = _compose(user_id, text)
        turn = await chat.start_turn(user_id, username, text, regenerate=regenerate)
        placeholder = await bot.send_message(chat_id, "⏳ Думаю…", reply_markup=STOP_KB)
        typing = asyncio.create_task(_typing_loop(bot, chat_id))
        base_messages = _with_tool_prompt(turn.messages, specs, is_admin)
        next_edit = 0.0

        async def refresh(status: str = "", force: bool = False) -> None:
            nonlocal next_edit
            now = time.monotonic()
            if not force and now < next_edit:
                return
            next_edit = now + settings.stream_edit_interval
            shown = (full + ("\n\n" if full and status else "") + status) or "…"
            html_text, plain = render_chunks(shown)[-1]  # во время стрима показываем последнюю часть
            try:
                await edit(placeholder, html_text, plain, STOP_KB)  # type: ignore[arg-type]
            except TelegramRetryAfter as exc:
                next_edit = time.monotonic() + exc.retry_after + 0.5

        async def consume() -> None:
            nonlocal full
            work = list(base_messages)
            for step in range(settings.max_tool_steps + 1):
                calls: list[dict[str, str]] | None = None
                step_text = ""
                allow_tools = bool(specs) and step < settings.max_tool_steps
                async for kind, value in stream_chat(
                    work,
                    model=turn.model,  # type: ignore[union-attr]
                    temperature=turn.temperature,  # type: ignore[union-attr]
                    max_tokens=turn.max_tokens,  # type: ignore[union-attr]
                    tools=specs if allow_tools else None,
                ):
                    if kind == "text":
                        if not step_text and full:
                            full += "\n\n"  # текст из разных шагов агента разделяем абзацем
                        step_text += value
                        full += value
                        await refresh()
                    else:
                        calls = value
                if not calls:
                    return
                work.append(
                    {
                        "role": "assistant",
                        "content": step_text or None,
                        "tool_calls": [
                            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                            for c in calls
                        ],
                    }
                )
                for c in calls:
                    await refresh(tools.TOOL_STATUS.get(c["name"], "🛠 Работаю…"), force=True)
                    result = await tools.execute(ctx, c["name"], c["arguments"])
                    work.append({"role": "tool", "tool_call_id": c["id"], "content": result})

        worker = asyncio.create_task(consume())
        waiter = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({worker, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            worker.cancel()
            raise
        finally:
            waiter.cancel()
        if worker.done():
            worker.result()  # пробрасывает LLMError и прочие ошибки
        else:  # нажали «Стоп»
            stopped = True
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
    except chat.PaymentRequired as exc:
        error, payment_kb = exc.text, buy_kb()
    except LookupError:
        error = "Нечего повторять: в текущем диалоге нет вашего сообщения."
    except LLMError as exc:
        error = exc.user_message
    except Exception:  # noqa: BLE001
        log.exception("Unexpected error in chat handler")
        error = "Внутренняя ошибка. Попробуйте ещё раз."
    finally:
        if typing:
            typing.cancel()
        chat.release(user_id)
        chat.cancel_events.pop(user_id, None)

    produced = bool(ctx.made)
    if turn is not None:
        if full.strip() or produced:  # сохраняем всё, что успели получить (даже при ошибке или «Стоп»)
            footer = f"\n\n[Создано файлов: {', '.join(ctx.made)}]" if produced else ""
            with contextlib.suppress(Exception):
                await chat.finish_turn(turn.conversation_id, (full.strip() or "(выполнено)") + footer)
        else:  # результата нет — возвращаем списанные ⭐
            with contextlib.suppress(Exception):
                await chat.refund(user_id, turn.cost)

    final = full
    if stopped:
        final = (final + "\n\n⏹ Остановлено").strip()
    if error:
        final = (final + f"\n\n⚠️ {error}").strip()
    if not final.strip():
        final = "✅ Готово." if produced else "Пустой ответ от модели."

    if error and payment_kb is None:
        markup = _retry_kb()
    elif payment_kb is not None:
        markup = payment_kb
    else:
        markup = _after_kb(ctx.need_topup)

    if placeholder is None:
        await bot.send_message(chat_id, final, reply_markup=markup)
        return
    chunks = render_chunks(final)
    for i, (html_text, plain) in enumerate(chunks):
        is_last = i == len(chunks) - 1
        if i == 0:
            await with_flood_retry(edit, placeholder, html_text, plain, markup if is_last else EMPTY_KB)
        else:
            await send(bot, chat_id, html_text, plain, markup if is_last else None)


# ───────────── хэндлеры ─────────────


@router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def on_text(m: Message, bot: Bot) -> None:
    assert m.from_user and m.text
    if len(m.text) > MAX_USER_TEXT:
        await m.answer(f"Слишком длинное сообщение ({len(m.text)} из {MAX_USER_TEXT} символов). Пришлите длинный текст файлом.")
        return
    await run_turn(bot, m.chat.id, m.from_user.id, m.from_user.username, m.text)


@router.callback_query(F.data == "stop")
async def cb_stop(cb: CallbackQuery) -> None:
    event = chat.cancel_events.get(cb.from_user.id)
    if event is None:
        await cb.answer("Генерация уже завершена")
        return
    event.set()
    await cb.answer("Останавливаю…")


@router.callback_query(F.data == "regen")
async def cb_regen(cb: CallbackQuery, bot: Bot) -> None:
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await run_turn(bot, cb.from_user.id, cb.from_user.id, cb.from_user.username, None, regenerate=True)


@router.message(Command("admin"))
async def admin_denied(m: Message) -> None:  # админы обрабатываются раньше, в admin.router
    await m.answer("Эта команда недоступна.")


@router.message(StateFilter(None), ~F.text)
async def on_other(m: Message) -> None:
    await m.answer("Я понимаю текст, фото и документы. Пришлите сообщение или файл.")
