"""Настройки: модель, температура, длина ответа, системный промт, роли."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.personas import PERSONAS
from bot.states import Form
from bot.utils import btn, esc, grid, kb, load_user, nav_row, pages, render
from config import settings
from db import repo
from db.session import SessionLocal
from llm.client import list_models
from services import billing

router = Router(name="prefs")
SYSTEM_MAX = 4000
MODELS_PER_PAGE = 8
TEMPS = [0.0, 0.3, 0.7, 1.0, 1.3, 1.7, 2.0]
MAX_TOKENS_OPTIONS = [0, 256, 512, 1024, 2048, 4096]


async def _set(user_id: int, username: str | None, **fields) -> None:
    async with SessionLocal() as s:
        user = await repo.get_or_create_user(s, user_id, username)
        for key, value in fields.items():
            setattr(user, key, value)
        await s.commit()


# ───────────── экран настроек ─────────────


async def show_settings(event: Message | CallbackQuery) -> None:
    user = await load_user(event.from_user.id)  # type: ignore[union-attr]
    model = user.model or settings.openai_model
    prompt = user.system_prompt
    prompt_line = esc(prompt[:120] + ("…" if len(prompt) > 120 else "")) if prompt else "по умолчанию"
    await render(
        event,
        "<b>⚙️ Настройки</b>\n"
        f"🤖 Модель: <code>{esc(model)}</code> — {billing.price_label(model)}\n"
        f"🌡 Температура: {user.temperature:g}\n"
        f"📏 Длина ответа: {f'{user.max_tokens} токенов' if user.max_tokens else 'авто'}\n"
        f"📝 Системный промт: {prompt_line}",
        kb(
            [btn("🤖 Модель", "s:model"), btn("🌡 Температура", "s:temp")],
            [btn("📏 Длина ответа", "s:max"), btn("📝 Промт", "s:sys")],
            [btn("🎭 Роли", "s:persona"), btn("◀️ Меню", "m:menu")],
        ),
    )


@router.message(Command("settings"))
async def cmd_settings(m: Message) -> None:
    await show_settings(m)


@router.callback_query(F.data == "m:settings")
async def cb_settings(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_settings(cb)


# ───────────── модель ─────────────


async def _visible_models() -> list[str]:
    return [m for m in await list_models() if billing.is_enabled(m)]


async def show_models(event: Message | CallbackQuery, page: int = 0) -> None:
    models = await _visible_models()
    current = (await load_user(event.from_user.id)).model or settings.openai_model  # type: ignore[union-attr]
    total = pages(len(models), MODELS_PER_PAGE)
    page = min(max(page, 0), total - 1)
    rows = [
        [btn(("✅ " if models[i] == current else "") + f"{models[i]} · {billing.price_label(models[i])}", f"ms:{i}")]
        for i in range(page * MODELS_PER_PAGE, min(len(models), (page + 1) * MODELS_PER_PAGE))
    ]
    if total > 1:
        rows.append(nav_row("mp:", page, total))
    rows.append([btn("⭐ Баланс", "m:bal"), btn("◀️ Назад", "m:settings")])
    await render(
        event,
        f"🤖 <b>Выбор модели</b>\nСейчас: <code>{esc(current)}</code>\n"
        "Цена указана за сообщение; платные модели списывают ⭐ с баланса.",
        kb(*rows),
    )


@router.message(Command("model"))
async def cmd_model(m: Message) -> None:
    await show_models(m)


@router.callback_query(F.data == "s:model")
async def cb_model(cb: CallbackQuery) -> None:
    await show_models(cb)


@router.callback_query(F.data.startswith("mp:"))
async def cb_model_page(cb: CallbackQuery) -> None:
    await show_models(cb, int((cb.data or "mp:0").split(":")[1]))


@router.callback_query(F.data.startswith("ms:"))
async def cb_model_select(cb: CallbackQuery) -> None:
    models = await _visible_models()
    idx = int((cb.data or "ms:0").split(":")[1])
    if idx >= len(models):
        await cb.answer("Список моделей обновился — откройте его заново.", show_alert=True)
        return
    await _set(cb.from_user.id, cb.from_user.username, model=models[idx])
    await show_settings(cb)


# ───────────── температура и длина ответа ─────────────


@router.callback_query(F.data == "s:temp")
async def cb_temp(cb: CallbackQuery) -> None:
    current = (await load_user(cb.from_user.id)).temperature
    buttons = [btn(("✅ " if abs(t - current) < 1e-9 else "") + f"{t:g}", f"s:t:{t}") for t in TEMPS]
    await render(
        cb,
        "🌡 <b>Температура</b>\nНиже — точнее и суше (код, факты), выше — разнообразнее и креативнее.",
        kb(*grid(buttons, 4), [btn("◀️ Назад", "m:settings")]),
    )


@router.callback_query(F.data.startswith("s:t:"))
async def cb_temp_set(cb: CallbackQuery) -> None:
    value = float((cb.data or "").split(":")[2])
    if value in TEMPS:
        await _set(cb.from_user.id, cb.from_user.username, temperature=value)
    await show_settings(cb)


@router.callback_query(F.data == "s:max")
async def cb_max(cb: CallbackQuery) -> None:
    current = (await load_user(cb.from_user.id)).max_tokens or 0
    buttons = [btn(("✅ " if n == current else "") + ("Авто" if n == 0 else str(n)), f"s:x:{n}") for n in MAX_TOKENS_OPTIONS]
    await render(
        cb,
        "📏 <b>Максимальная длина ответа</b> (в токенах)\nЭто верхняя граница: короче — быстрее и дешевле.",
        kb(*grid(buttons, 3), [btn("✍️ Своё значение", "s:xc")], [btn("◀️ Назад", "m:settings")]),
    )


@router.callback_query(F.data.startswith("s:x:"))
async def cb_max_set(cb: CallbackQuery) -> None:
    n = int((cb.data or "").split(":")[2])
    await _set(cb.from_user.id, cb.from_user.username, max_tokens=n or None)
    await show_settings(cb)


@router.callback_query(F.data == "s:xc")
async def cb_max_custom(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.max_tokens)
    await render(cb, "✍️ Отправьте число от 1 до 32000.\n/cancel — отмена", kb([btn("◀️ Назад", "s:max")]))


@router.message(Form.max_tokens, F.text)
async def on_max_custom(m: Message, state: FSMContext) -> None:
    assert m.from_user
    raw = (m.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 32000:
        await m.answer("Нужно целое число от 1 до 32000. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    await _set(m.from_user.id, m.from_user.username, max_tokens=int(raw))
    await show_settings(m)


# ───────────── системный промт и роли ─────────────


@router.callback_query(F.data == "s:sys")
async def cb_sys(cb: CallbackQuery) -> None:
    user = await load_user(cb.from_user.id)
    current = user.system_prompt or settings.default_system_prompt
    await render(
        cb,
        f"📝 <b>Системный промт</b>{'' if user.system_prompt else ' (по умолчанию)'}\n<blockquote>{esc(current[:1500])}</blockquote>",
        kb(
            [btn("✏️ Изменить", "s:sysedit"), btn("♻️ Сбросить", "s:sysreset")],
            [btn("🎭 Роли", "s:persona"), btn("◀️ Назад", "m:settings")],
        ),
    )


@router.callback_query(F.data == "s:sysedit")
async def cb_sys_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.system)
    await render(cb, f"✏️ Отправьте новый системный промт (до {SYSTEM_MAX} символов).\n/cancel — отмена", kb([btn("◀️ Назад", "s:sys")]))


@router.message(Form.system, F.text)
async def on_sys(m: Message, state: FSMContext) -> None:
    assert m.from_user
    text = (m.text or "").strip()
    if len(text) > SYSTEM_MAX:
        await m.answer(f"Слишком длинно ({len(text)} из {SYSTEM_MAX}). Сократите или /cancel.")
        return
    await state.clear()
    await _set(m.from_user.id, m.from_user.username, system_prompt=text or None)
    await show_settings(m)


@router.callback_query(F.data == "s:sysreset")
async def cb_sys_reset(cb: CallbackQuery) -> None:
    await _set(cb.from_user.id, cb.from_user.username, system_prompt=None)
    await show_settings(cb)


@router.message(Command("system"))
async def cmd_system(m: Message, command: CommandObject) -> None:
    """Быстрый вариант: /system текст  |  /system reset"""
    assert m.from_user
    arg = (command.args or "").strip()
    if not arg:
        await render(m, "Задайте промт: <code>/system текст</code>, сбросьте: <code>/system reset</code> — или откройте /settings.", kb([btn("📝 Промт", "s:sys")]))
        return
    if len(arg) > SYSTEM_MAX:
        await m.answer(f"Слишком длинный промт (максимум {SYSTEM_MAX} символов).")
        return
    reset = arg.lower() in {"reset", "сброс"}
    await _set(m.from_user.id, m.from_user.username, system_prompt=None if reset else arg)
    await m.answer("Системный промт сброшен." if reset else "Системный промт обновлён.")


async def show_personas(event: Message | CallbackQuery) -> None:
    prompt = (await load_user(event.from_user.id)).system_prompt  # type: ignore[union-attr]
    active = next((k for k, (_, p) in PERSONAS.items() if p == prompt), None)
    buttons = [btn(("✅ " if key == active else "") + title, f"p:{key}") for key, (title, _) in PERSONAS.items()]
    rows = grid(buttons, 2)
    rows.append([btn(("✅ " if not prompt else "") + "🙂 Универсальный", "p:default")])
    rows.append([btn("◀️ Назад", "m:settings")])
    await render(event, "🎭 <b>Роли ассистента</b>\nРоль задаёт стиль и специализацию ответов (меняет системный промт).", kb(*rows))


@router.message(Command("persona"))
async def cmd_persona(m: Message) -> None:
    await show_personas(m)


@router.callback_query(F.data == "s:persona")
async def cb_persona(cb: CallbackQuery) -> None:
    await show_personas(cb)


@router.callback_query(F.data.startswith("p:"))
async def cb_persona_set(cb: CallbackQuery) -> None:
    key = (cb.data or "").split(":")[1]
    if key == "default":
        await _set(cb.from_user.id, cb.from_user.username, system_prompt=None)
    elif key in PERSONAS:
        await _set(cb.from_user.id, cb.from_user.username, system_prompt=PERSONAS[key][1])
    else:
        await cb.answer()
        return
    await show_personas(cb)
