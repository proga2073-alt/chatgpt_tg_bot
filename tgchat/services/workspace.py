"""Рабочая папка пользователя: загруженные файлы и всё, что создала модель. Изолирована по user_id."""
from __future__ import annotations

import re
import time
from pathlib import Path

from config import settings

ROOT = Path(settings.workspace_dir)
MAX_FILES = 60  # старые файлы удаляются
_new_uploads: dict[int, list[str]] = {}


def user_dir(user_id: int) -> Path:
    d = ROOT / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    """Только имя файла: без путей, '..' и спецсимволов — выйти из папки нельзя."""
    name = Path(name.replace("\\", "/")).name.strip()
    name = re.sub(r"[^\w.\- ()]", "_", name)[:100].strip(". ")
    return name or "file"


def resolve(user_id: int, name: str) -> Path | None:
    path = user_dir(user_id) / safe_name(name)
    return path if path.is_file() else None


def save(user_id: int, name: str, data: bytes) -> Path:
    d = user_dir(user_id)
    path = d / safe_name(name)
    path.write_bytes(data)
    files = sorted((f for f in d.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
    for old in files[:-MAX_FILES]:
        old.unlink(missing_ok=True)
    return path


def unique_name(prefix: str, ext: str) -> str:
    return f"{prefix}_{int(time.time() * 1000) % 10**10}{ext}"


def list_files(user_id: int) -> list[tuple[str, int]]:
    d = user_dir(user_id)
    files = sorted((f for f in d.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
    return [(f.name, f.stat().st_size) for f in files]


def clear(user_id: int) -> int:
    n = 0
    for f in user_dir(user_id).iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)
            n += 1
    return n


def note_upload(user_id: int, name: str) -> None:
    _new_uploads.setdefault(user_id, []).append(name)


def pop_uploads(user_id: int) -> list[str]:
    return _new_uploads.pop(user_id, [])
