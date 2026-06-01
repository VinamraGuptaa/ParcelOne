"""Auth API tests: registration, login, sessions, and per-user workflow isolation."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from api.app import create_app
from api.database import Base, get_db
from api.models import LandCaseWorkflow, AuthSession, User

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def auth_engine():
    eng = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def auth_client(auth_engine, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("AUTH_ALLOW_REGISTER", "1")
    monkeypatch.setenv("AUTH_SESSION_MAX_AGE", "3600")

    Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    with patch("api.routes.jobs.run_scrape_job", new_callable=AsyncMock), patch(
        "api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac

    app.dependency_overrides.clear()


async def _register(client: AsyncClient, email: str, password: str = "secret123") -> dict:
    resp = await client.post(
        "/api/auth/register",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _login(client: AsyncClient, email: str, password: str = "secret123") -> dict:
    resp = await client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _auth_client_factory(engine, monkeypatch):
    """Build a fresh authenticated client (separate cookie jar)."""

    async def _factory():
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("AUTH_ALLOW_REGISTER", "1")
        monkeypatch.setenv("AUTH_SESSION_MAX_AGE", "3600")

        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async def override_get_db():
            async with Session() as session:
                yield session

        app = create_app()
        app.dependency_overrides[get_db] = override_get_db

        with patch("api.routes.jobs.run_scrape_job", new_callable=AsyncMock), patch(
            "api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock
        ):
            ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            await ac.__aenter__()
            try:
                yield ac
            finally:
                await ac.__aexit__(None, None, None)
                app.dependency_overrides.clear()

    return _factory


class TestAuthConfig:
    async def test_config_when_auth_disabled(self, client: AsyncClient):
        resp = await client.get("/api/auth/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["auth_enabled"] is False

    async def test_config_enabled_by_default_when_dev_zero(self, auth_engine, monkeypatch):
        """Docker/AWS: DEV=0 and no AUTH_ENABLED → auth on."""
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        monkeypatch.setenv("DEV", "0")

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)

        async def override_get_db():
            async with Session() as session:
                yield session

        app = create_app()
        app.dependency_overrides[get_db] = override_get_db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/auth/config")
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert resp.json()["auth_enabled"] is True

    async def test_config_when_auth_enabled(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/auth/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["auth_enabled"] is True
        assert body["allow_register"] is True


class TestAuthRegistration:
    async def test_register_creates_user_and_session(self, auth_client: AsyncClient, auth_engine):
        body = await _register(auth_client, "alice@example.com")
        assert body["email"] == "alice@example.com"
        assert "user_id" in body

        me = await auth_client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == "alice@example.com"

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            sessions = (await db.execute(select(AuthSession))).scalars().all()
            assert len(sessions) == 1
            assert sessions[0].token_hash
            exp = sessions[0].expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            assert exp > datetime.now(timezone.utc)

    async def test_register_duplicate_email_returns_409(self, auth_client: AsyncClient):
        await _register(auth_client, "dup@example.com")
        resp = await auth_client.post(
            "/api/auth/register",
            json={"email": "dup@example.com", "password": "secret123"},
        )
        assert resp.status_code == 409

    async def test_register_rejects_short_password(self, auth_client: AsyncClient):
        resp = await auth_client.post(
            "/api/auth/register",
            json={"email": "short@example.com", "password": "abc"},
        )
        assert resp.status_code == 422

    async def test_register_normalizes_email(self, auth_client: AsyncClient):
        body = await _register(auth_client, "  BOB@Example.COM  ")
        assert body["email"] == "bob@example.com"


class TestAuthLogin:
    async def test_login_existing_user(self, auth_client: AsyncClient):
        await _register(auth_client, "login@example.com")
        await auth_client.post("/api/auth/logout")
        body = await _login(auth_client, "login@example.com")
        assert body["email"] == "login@example.com"

    async def test_login_bad_password_returns_401(self, auth_client: AsyncClient):
        await _register(auth_client, "badpw@example.com")
        await auth_client.post("/api/auth/logout")
        resp = await auth_client.post(
            "/api/auth/login",
            json={"email": "badpw@example.com", "password": "wrong-password"},
        )
        assert resp.status_code == 401

    async def test_login_unknown_email_returns_401(self, auth_client: AsyncClient):
        resp = await auth_client.post(
            "/api/auth/login",
            json={"email": "missing@example.com", "password": "secret123"},
        )
        assert resp.status_code == 401


class TestAuthSessions:
    async def test_protected_route_requires_session(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/workflows")
        assert resp.status_code == 401

    async def test_health_public_without_session(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/health")
        assert resp.status_code == 200

    async def test_logout_deletes_session_and_invalidates_cookie(self, auth_client: AsyncClient, auth_engine):
        await _register(auth_client, "logout@example.com")
        assert (await auth_client.get("/api/auth/me")).status_code == 200

        logout = await auth_client.post("/api/auth/logout")
        assert logout.status_code == 200

        me = await auth_client.get("/api/auth/me")
        assert me.status_code == 401

        workflows = await auth_client.get("/api/workflows")
        assert workflows.status_code == 401

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            count = len((await db.execute(select(AuthSession))).scalars().all())
            assert count == 0

    async def test_expired_session_returns_401(self, auth_client: AsyncClient, auth_engine):
        await _register(auth_client, "expired@example.com")

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            row = (await db.execute(select(AuthSession))).scalar_one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

        me = await auth_client.get("/api/auth/me")
        assert me.status_code == 401

    async def test_token_hash_is_sha256_not_raw_token(self, auth_client: AsyncClient, auth_engine):
        await _register(auth_client, "hash@example.com")
        raw = auth_client.cookies.get("session_token")
        assert raw

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            row = (await db.execute(select(AuthSession))).scalar_one()
            assert row.token_hash == hashlib.sha256(raw.encode()).hexdigest()
            assert row.token_hash != raw


class TestAuthWorkflowIsolation:
    async def test_workflows_scoped_to_current_user(self, auth_engine, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("AUTH_ALLOW_REGISTER", "1")

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)

        async def make_client():
            async def override_get_db():
                async with Session() as session:
                    yield session

            app = create_app()
            app.dependency_overrides[get_db] = override_get_db
            ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            await ac.__aenter__()
            return app, ac

        import api.routes.workflows as workflows_routes

        app_a, client_a = await make_client()
        try:
            with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock), patch.object(
                workflows_routes,
                "ACTIVE_WORKFLOW_STATUSES",
                ("bhulekh_running", "name_variants_ready", "igr_running", "ecourts_running"),
            ):
                await _register(client_a, "usera@example.com")
                create_a = await client_a.post(
                    "/api/workflows/land-case-search",
                    json={
                        "district_label": "Pune",
                        "taluka_label": "Haveli",
                        "village_label": "Wagholi",
                        "survey_part1": "1530",
                        "survey_option_label": "1530/3",
                    },
                )
            assert create_a.status_code == 202
            wf_a = create_a.json()["workflow_id"]

            list_a = await client_a.get("/api/workflows")
            assert list_a.status_code == 200
            assert len(list_a.json()["workflows"]) == 1
        finally:
            await client_a.__aexit__(None, None, None)
            app_a.dependency_overrides.clear()

        app_b, client_b = await make_client()
        try:
            await _register(client_b, "userb@example.com")
            list_b = await client_b.get("/api/workflows")
            assert list_b.status_code == 200
            assert len(list_b.json()["workflows"]) == 0

            get_other = await client_b.get(f"/api/workflows/{wf_a}")
            assert get_other.status_code == 404
        finally:
            await client_b.__aexit__(None, None, None)
            app_b.dependency_overrides.clear()

    async def test_active_workflow_guard_is_per_user(self, auth_engine, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("AUTH_ALLOW_REGISTER", "1")

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)

        async def make_client():
            async def override_get_db():
                async with Session() as session:
                    yield session

            app = create_app()
            app.dependency_overrides[get_db] = override_get_db
            ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            await ac.__aenter__()
            return app, ac

        import api.routes.workflows as workflows_routes

        app_a, client_a = await make_client()
        try:
            await _register(client_a, "activea@example.com")
            async with Session() as db:
                user_a = (await db.execute(select(User).where(User.email == "activea@example.com"))).scalar_one()
                db.add(
                    LandCaseWorkflow(
                        user_id=user_a.id,
                        district_label="Pune",
                        taluka_label="Haveli",
                        village_label="Wagholi",
                        survey_part1="1530",
                        survey_option_label="1530/3",
                        status="igr_running",
                    )
                )
                await db.commit()

            with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock), patch.object(
                workflows_routes,
                "ACTIVE_WORKFLOW_STATUSES",
                ("igr_running",),
            ):
                blocked = await client_a.post(
                    "/api/workflows/land-case-search",
                    json={
                        "district_label": "Pune",
                        "taluka_label": "Haveli",
                        "village_label": "Wagholi",
                        "survey_part1": "204",
                        "survey_option_label": "204/1",
                    },
                )
            assert blocked.status_code == 409
        finally:
            await client_a.__aexit__(None, None, None)
            app_a.dependency_overrides.clear()

        app_b, client_b = await make_client()
        try:
            with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock), patch.object(
                workflows_routes,
                "ACTIVE_WORKFLOW_STATUSES",
                ("igr_running",),
            ):
                await _register(client_b, "activeb@example.com")
                ok = await client_b.post(
                    "/api/workflows/land-case-search",
                    json={
                        "district_label": "Pune",
                        "taluka_label": "Haveli",
                        "village_label": "Wagholi",
                        "survey_part1": "204",
                        "survey_option_label": "204/1",
                    },
                )
            assert ok.status_code == 202
        finally:
            await client_b.__aexit__(None, None, None)
            app_b.dependency_overrides.clear()


class TestAuthDisabled:
    async def test_workflows_public_when_auth_disabled(self, client: AsyncClient):
        import api.routes.workflows as workflows_routes

        with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock), patch.object(
            workflows_routes,
            "ACTIVE_WORKFLOW_STATUSES",
            ("bhulekh_running", "name_variants_ready", "igr_running", "ecourts_running"),
        ):
            resp = await client.post(
                "/api/workflows/land-case-search",
                json={
                    "district_label": "Pune",
                    "taluka_label": "Haveli",
                    "village_label": "Wagholi",
                    "survey_part1": "1530",
                    "survey_option_label": "1530/3",
                },
            )
        assert resp.status_code == 202

        listed = await client.get("/api/workflows")
        assert listed.status_code == 200


class TestAdminAccount:
    async def test_ensure_admin_account_creates_admin(self, auth_engine, monkeypatch):
        monkeypatch.setenv("AUTH_ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("AUTH_ADMIN_PASSWORD", "admin-secret")

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            from api.auth import ensure_admin_account

            await ensure_admin_account(db)
            user = (await db.execute(select(User).where(User.email == "admin@example.com"))).scalar_one()
            assert user.is_admin is True

    async def test_admin_login_returns_is_admin(self, auth_engine, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("AUTH_ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("AUTH_ADMIN_PASSWORD", "admin-secret")

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            from api.auth import ensure_admin_account

            await ensure_admin_account(db)

        async def override_get_db():
            async with Session() as session:
                yield session

        app = create_app()
        app.dependency_overrides[get_db] = override_get_db

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "admin-secret"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["is_admin"] is True

            me = await client.get("/api/auth/me")
            assert me.json()["is_admin"] is True

        app.dependency_overrides.clear()

    async def test_register_never_creates_admin(self, auth_client: AsyncClient):
        body = await _register(auth_client, "regular@example.com")
        assert body["is_admin"] is False
