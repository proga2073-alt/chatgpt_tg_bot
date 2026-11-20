"""Async-движок и фабрика сессий. Для PostgreSQL достаточно поменять DATABASE_URL."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import settings
from db.models import Base


def _prepare_sqlite_dir(url: str) -> None:
    u = make_url(url)
    if u.get_backend_name() == "sqlite" and u.database and u.database != ":memory:":
        Path(u.database).parent.mkdir(parents=True, exist_ok=True)


_prepare_sqlite_dir(settings.database_url)
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


_NEW_COLUMNS = [  # (таблица, колонка, DDL) — для баз, созданных старыми версиями
    ("users", "balance", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "banned", "BOOLEAN NOT NULL DEFAULT {false}"),
    ("payments", "refunded", "BOOLEAN NOT NULL DEFAULT {false}"),
]


def _migrate(sync_conn) -> None:
    insp = inspect(sync_conn)
    false = "FALSE" if sync_conn.dialect.name == "postgresql" else "0"
    for table, column, ddl in _NEW_COLUMNS:
        if insp.has_table(table) and column not in {c["name"] for c in insp.get_columns(table)}:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl.format(false=false)}"))


async def init_db() -> None:
    """Создаёт таблицы и добавляет недостающие колонки. Для серьёзной эксплуатации подключите Alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)
