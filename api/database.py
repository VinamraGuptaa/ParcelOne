"""
Async SQLAlchemy database setup.

Supports both PostgreSQL (Supabase) and SQLite (local dev).
Set DATABASE_URL in your .env file:
  - PostgreSQL: postgresql+asyncpg://user:pass@host:port/dbname
  - SQLite:     sqlite+aiosqlite:///./ecourts.db
"""

import os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import event


def _get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./ecourts.db")
    # Render/Supabase sometimes give postgresql:// — fix dialect for asyncpg
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
    from sqlalchemy import event as _event

    @_event.listens_for(engine.sync_engine, "connect")
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
