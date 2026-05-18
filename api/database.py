"""
Async SQLAlchemy database setup.

Supports both PostgreSQL (Supabase) and SQLite (local dev).
Set DATABASE_URL in your .env file:
  - PostgreSQL: postgresql+asyncpg://user:pass@host:port/dbname
  - SQLite:     sqlite+aiosqlite:///./ecourts.db
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)


def _get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./ecourts.db")
    # Cloud providers often provide sync postgres DSNs; normalize for asyncpg.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


DATABASE_URL = _get_database_url()

# Use WAL mode for SQLite (allows concurrent reads while writing)
_connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=_connect_args,
)

# For SQLite: enable WAL mode on connect
if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _set_wal(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA synchronous=NORMAL")


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency: yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


# Structured columns on workflow_igr_hits (added after initial release).
WORKFLOW_IGR_HIT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("doc_no", "TEXT"),
    ("doc_type", "TEXT"),
    ("doc_type_marathi", "TEXT"),
    ("reg_date", "TEXT"),
    ("seller_name", "TEXT"),
    ("buyer_name", "TEXT"),
)

_igr_columns_ready = False


async def _table_exists(conn, table: str) -> bool:
    if DATABASE_URL.startswith("sqlite"):
        result = await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        )
        return result.scalar() is not None
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return result.scalar() is not None


async def _column_exists(conn, table: str, column: str) -> bool:
    if DATABASE_URL.startswith("sqlite"):
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        return any(row[1] == column for row in result.fetchall())
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.scalar() is not None


async def ensure_workflow_igr_hit_columns() -> None:
    """Add missing workflow_igr_hits columns (safe to call repeatedly)."""
    global _igr_columns_ready
    if _igr_columns_ready:
        return

    async with engine.begin() as conn:
        if not await _table_exists(conn, "workflow_igr_hits"):
            return

        for col_name, col_type in WORKFLOW_IGR_HIT_COLUMNS:
            if await _column_exists(conn, "workflow_igr_hits", col_name):
                continue
            await conn.execute(
                text(f"ALTER TABLE workflow_igr_hits ADD COLUMN {col_name} {col_type}")
            )
            logger.info("Applied DB migration: workflow_igr_hits.%s", col_name)

    _igr_columns_ready = True


# Backward-compatible alias used by tests / older imports.
async def run_column_migrations() -> None:
    await ensure_workflow_igr_hit_columns()
