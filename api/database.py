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


# New structured columns added to workflow_igr_hits. Existing rows retain NULL;
# the route falls back to parsing raw_json for those rows.
_IGR_HIT_MIGRATIONS = [
    "ALTER TABLE workflow_igr_hits ADD COLUMN doc_no TEXT",
    "ALTER TABLE workflow_igr_hits ADD COLUMN doc_type TEXT",
    "ALTER TABLE workflow_igr_hits ADD COLUMN doc_type_marathi TEXT",
    "ALTER TABLE workflow_igr_hits ADD COLUMN reg_date TEXT",
    "ALTER TABLE workflow_igr_hits ADD COLUMN seller_name TEXT",
    "ALTER TABLE workflow_igr_hits ADD COLUMN buyer_name TEXT",
]


async def run_column_migrations() -> None:
    """Idempotently add any missing columns to existing tables.

    Uses try/except so it is safe to call on every startup regardless of
    whether the migration has already been applied.
    """
    from sqlalchemy import text

    async with engine.begin() as conn:
        for stmt in _IGR_HIT_MIGRATIONS:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists
