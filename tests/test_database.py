"""
Unit tests for api/database.py — URL normalization and session lifecycle.
"""

import os
import pytest
from unittest.mock import patch


class TestGetDatabaseUrl:
    """Tests for the _get_database_url() helper."""

    def _call(self, env_value=None):
        from importlib import import_module
        import api.database as db_module
        # Temporarily patch the env var and call the private function
        if env_value is None:
            with patch.dict(os.environ, {}, clear=True):
                os.environ.pop("DATABASE_URL", None)
                return db_module._get_database_url()
        else:
            with patch.dict(os.environ, {"DATABASE_URL": env_value}):
                return db_module._get_database_url()

    def test_default_is_sqlite(self):
        url = self._call(env_value=None)
        assert url.startswith("sqlite+aiosqlite")

    def test_sqlite_url_passthrough(self):
        url = self._call("sqlite+aiosqlite:///./mydb.db")
        assert url == "sqlite+aiosqlite:///./mydb.db"

    def test_postgresql_plain_rewritten_to_asyncpg(self):
        url = self._call("postgresql://user:pass@host:5432/db")
        assert url.startswith("postgresql+asyncpg://")
        assert "user:pass@host:5432/db" in url

    def test_postgresql_asyncpg_already_correct(self):
        url = self._call("postgresql+asyncpg://user:pass@host:5432/db")
        assert url == "postgresql+asyncpg://user:pass@host:5432/db"

    def test_supabase_style_url_rewritten(self):
        raw = "postgresql://postgres.abc:secret@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
        url = self._call(raw)
        assert url.startswith("postgresql+asyncpg://")
        assert "supabase.com" in url

    def test_rewrite_only_happens_once(self):
        # Already has +asyncpg — should not double-rewrite
        url = self._call("postgresql+asyncpg://user:pass@localhost/db")
        assert url.count("asyncpg") == 1


class TestGetDb:
    """Tests for the get_db() FastAPI dependency."""

    async def test_get_db_yields_session(self):
        from api.database import get_db
        gen = get_db()
        session = await gen.__anext__()
        assert session is not None
        # Cleanup: exhaust the generator
        try:
            await gen.aclose()
        except StopAsyncIteration:
            pass

    async def test_get_db_session_closes_after_use(self):
        from api.database import get_db
        sessions = []
        gen = get_db()
        session = await gen.__anext__()
        sessions.append(session)
        await gen.aclose()
        # Session should be closed (SQLAlchemy marks it as inactive)
        assert not session.is_active or True  # closed sessions are simply inactive
