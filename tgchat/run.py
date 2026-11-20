#!/usr/bin/env python3
"""Запуск бота. При первом запуске спрашивает OPENAI_API_KEY, BOT_TOKEN и ADMIN_IDS и сохраняет их в .env.

    python run.py                 # обычный запуск
    python run.py --reconfigure   # заново ввести ключи
"""
from __future__ import annotations

import getpass
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

TOKEN_RE = re.compile(r"^\d{5,}:[\w-]{30,}$")  # формат токена от @BotFather
PLACEHOLDER_MARKERS = ("your", "...", "example")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip("'\"")
    return values


def is_missing(value: str | None) -> bool:
    return not value or any(m in value.lower() for m in PLACEHOLDER_MARKERS)


def set_value(text: str, key: str, value: str) -> str:
    """Заменяет строку KEY=... (в т.ч. закомментированную) или добавляет в конец."""
    line = f"{key}={value}"
    pattern = re.compile(rf"^#?\s*{re.escape(key)}=.*$", re.M)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    return text.rstrip("\n") + f"\n{line}\n"


def ask_secret(prompt: str, validator=None, hint: str = "") -> str:
    while True:
        value = getpass.getpass(f"{prompt}: ").strip()  # ввод скрыт
        if not value:
            print("  Значение не может быть пустым.")
            continue
        if validator and not validator(value):
            print(f"  Похоже на неверный формат. {hint}")
            if input("  Всё равно использовать? [y/N] ").strip().lower() != "y":
                continue
        return value


def check_token(token: str) -> bool | None:
    """True/False — Telegram подтвердил/отверг токен; None — проверить не удалось (нет сети)."""
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=10) as r:
            data = json.load(r)
        if data.get("ok"):
            print(f"  Токен принят: @{data['result'].get('username')}")
            return True
        return False
    except urllib.error.HTTPError:
        return False  # 401/404 — токен неверный или отозван
    except Exception:  # noqa: BLE001
        print("  Не удалось проверить токен онлайн — пропускаю проверку.")
        return None


def ask_token() -> str:
    while True:
        token = ask_secret("BOT_TOKEN (из @BotFather)", TOKEN_RE.match, "Ожидается вид 123456789:AA...")
        token = token.removeprefix("bot")  # частая ошибка: токен скопирован из URL вместе с «bot»
        if check_token(token) is False:
            print("  Telegram отверг этот токен. Скопируйте его заново из @BotFather (/mybots → API Token).")
            continue
        return token


def ask_admin_ids() -> str:
    print("\nADMIN_IDS — ваш Telegram ID (число) для админ-панели. Узнать: написать боту @userinfobot.")
    print("Несколько админов — через запятую. Enter — без админ-панели (можно добавить позже в .env).")
    while True:
        raw = input("ADMIN_IDS: ").replace(" ", "")
        if not raw or re.fullmatch(r"\d+(,\d+)*", raw):
            return raw
        print("  Нужны только числа через запятую, например 123456789,987654321.")


def configure(force: bool) -> None:
    env = read_env(ENV_FILE)
    get = lambda k: os.environ.get(k) or env.get(k)  # noqa: E731 — переменные окружения приоритетнее файла
    need_key = force or is_missing(get("OPENAI_API_KEY"))
    need_token = force or is_missing(get("BOT_TOKEN"))
    need_admin = force or ("ADMIN_IDS" not in env and "ADMIN_IDS" not in os.environ)  # спрашиваем один раз
    if not (need_key or need_token or need_admin):
        return

    if not sys.stdin.isatty():
        missing = [k for k, n in (("OPENAI_API_KEY", need_key), ("BOT_TOKEN", need_token)) if n]
        if missing:
            sys.exit(f"Не заданы: {', '.join(missing)}. Заполните .env или переменные окружения (нет интерактивного терминала).")
        return

    print("Первичная настройка. Ввод ключей скрыт, данные сохраняются только в локальный .env\n")
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else (
        ENV_EXAMPLE.read_text(encoding="utf-8") if ENV_EXAMPLE.exists() else ""
    )
    if need_key:
        text = set_value(text, "OPENAI_API_KEY", ask_secret("OPENAI_API_KEY (ключ zenvy.su)"))
    if need_token:
        text = set_value(text, "BOT_TOKEN", ask_token())
    if need_admin:
        text = set_value(text, "ADMIN_IDS", ask_admin_ids())

    ENV_FILE.write_text(text, encoding="utf-8")
    try:
        ENV_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600: читать может только владелец
    except OSError:
        pass  # например, Windows
    print(f"\nГотово: настройки сохранены в {ENV_FILE.name}\n")


def main() -> None:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    configure(force="--reconfigure" in sys.argv)
    try:
        from main import main as run_app
    except ModuleNotFoundError as exc:
        sys.exit(f"Не хватает зависимости «{exc.name}». Выполните: pip install -r requirements.txt")
    run_app()


if __name__ == "__main__":
    main()
