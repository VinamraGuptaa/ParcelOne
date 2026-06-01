"""Auth edge cases: refresh simulation, bearer vs cookie, sliding sessions, public routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from api.app import create_app
from api.auth import _as_utc
from api.database import Base, get_db
from api.models import AuthSession, Case, SearchJob, User

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


def _bearer_client(transport, token: str) -> AsyncClient:
    """Simulates a fresh browser tab with only localStorage token (no cookies)."""
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


class TestSessionPersistenceEdgeCases:
    async def test_login_returns_session_token_for_storage(self, auth_client: AsyncClient):
        reg = await auth_client.post(
            "/api/auth/register",
            json={"email": "tok@example.com", "password": "secret123"},
        )
        assert reg.status_code == 200
        body = reg.json()
        assert body.get("session_token")
        me_body = (await auth_client.get("/api/auth/me")).json()
        assert not me_body.get("session_token")

    async def test_bearer_only_client_survives_refresh_simulation(self, auth_client: AsyncClient):
        """New client without cookies — like page reload using localStorage token."""
        reg = await auth_client.post(
            "/api/auth/register",
            json={"email": "refresh@example.com", "password": "secret123"},
        )
        token = reg.json()["session_token"]

        async with _bearer_client(auth_client._transport, token) as fresh:
            me = await fresh.get("/api/auth/me")
            assert me.status_code == 200
            assert me.json()["email"] == "refresh@example.com"

            workflows = await fresh.get("/api/workflows")
            assert workflows.status_code == 200

    async def test_sliding_session_extends_expiry_on_authenticated_request(
        self, auth_client: AsyncClient, auth_engine
    ):
        await auth_client.post(
            "/api/auth/register",
            json={"email": "slide@example.com", "password": "secret123"},
        )

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with Session() as db:
            row = (await db.execute(select(AuthSession))).scalar_one()
            row.expires_at = now + timedelta(seconds=20)
            await db.commit()

        assert (await auth_client.get("/api/auth/me")).status_code == 200

        async with Session() as db:
            row = (await db.execute(select(AuthSession))).scalar_one()
            assert _as_utc(row.expires_at) > now + timedelta(seconds=100)

    async def test_logout_revokes_bearer_token(self, auth_client: AsyncClient):
        reg = await auth_client.post(
            "/api/auth/register",
            json={"email": "revoke@example.com", "password": "secret123"},
        )
        token = reg.json()["session_token"]

        async with _bearer_client(auth_client._transport, token) as bearer:
            assert (await bearer.get("/api/auth/me")).status_code == 200
            assert (await bearer.post("/api/auth/logout")).status_code == 200
            assert (await bearer.get("/api/auth/me")).status_code == 401
            assert (await bearer.get("/api/workflows")).status_code == 401

    async def test_invalid_bearer_token_returns_401(self, auth_client: AsyncClient):
        async with _bearer_client(auth_client._transport, "not-a-valid-session") as bad:
            assert (await bad.get("/api/auth/me")).status_code == 401
            assert (await bad.get("/api/workflows")).status_code == 401

    async def test_expired_bearer_session_returns_401(self, auth_client: AsyncClient, auth_engine):
        reg = await auth_client.post(
            "/api/auth/register",
            json={"email": "exbearer@example.com", "password": "secret123"},
        )
        token = reg.json()["session_token"]

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            row = (await db.execute(select(AuthSession))).scalar_one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

        async with _bearer_client(auth_client._transport, token) as bearer:
            assert (await bearer.get("/api/auth/me")).status_code == 401

    async def test_second_login_invalidates_use_of_old_token_after_logout(
        self, auth_client: AsyncClient
    ):
        await auth_client.post(
            "/api/auth/register",
            json={"email": "relogin@example.com", "password": "secret123"},
        )
        old_token = (await auth_client.post(
            "/api/auth/login",
            json={"email": "relogin@example.com", "password": "secret123"},
        )).json()["session_token"]

        await auth_client.post("/api/auth/logout")

        async with _bearer_client(auth_client._transport, old_token) as stale:
            assert (await stale.get("/api/auth/me")).status_code == 401

        new_token = (await auth_client.post(
            "/api/auth/login",
            json={"email": "relogin@example.com", "password": "secret123"},
        )).json()["session_token"]

        async with _bearer_client(auth_client._transport, new_token) as fresh:
            assert (await fresh.get("/api/auth/me")).status_code == 200


class TestAuthPublicRoutes:
    async def test_config_and_health_public_without_session(self, auth_client: AsyncClient):
        async with AsyncClient(
            transport=auth_client._transport, base_url="http://test"
        ) as anon:
            assert (await anon.get("/api/auth/config")).status_code == 200
            assert (await anon.get("/api/health")).status_code == 200
            assert (await anon.get("/api/auth/me")).status_code == 401

    async def test_auth_login_register_public_when_enabled(self, auth_client: AsyncClient):
        async with AsyncClient(
            transport=auth_client._transport, base_url="http://test"
        ) as anon:
            reg = await anon.post(
                "/api/auth/register",
                json={"email": "public@example.com", "password": "secret123"},
            )
            assert reg.status_code == 200


class TestAuthRegistrationPolicy:
    async def test_register_closed_when_allow_register_disabled(
        self, auth_engine, monkeypatch
    ):
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("AUTH_ALLOW_REGISTER", "0")

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)

        async def override_get_db():
            async with Session() as session:
                yield session

        app = create_app()
        app.dependency_overrides[get_db] = override_get_db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/register",
                json={"email": "closed@example.com", "password": "secret123"},
            )
            assert resp.status_code == 403
            config = await client.get("/api/auth/config")
            assert config.json()["allow_register"] is False
        app.dependency_overrides.clear()


class TestAuthProtectedResources:
    async def test_jobs_list_requires_session(self, auth_client: AsyncClient):
        async with AsyncClient(
            transport=auth_client._transport, base_url="http://test"
        ) as anon:
            assert (await anon.get("/api/jobs")).status_code == 401

    async def test_jobs_scoped_after_bearer_login(self, auth_client: AsyncClient):
        reg = await auth_client.post(
            "/api/auth/register",
            json={"email": "jobs@example.com", "password": "secret123"},
        )
        token = reg.json()["session_token"]

        async with _bearer_client(auth_client._transport, token) as bearer:
            with patch("api.routes.jobs.run_scrape_job", new_callable=AsyncMock):
                create = await bearer.post(
                    "/api/jobs",
                    json={"petitioner_name": "Test Party", "year": "2020"},
                )
            assert create.status_code == 202

            listed = await bearer.get("/api/jobs")
            assert listed.status_code == 200
            assert len(listed.json()["jobs"]) == 1

    async def test_cases_list_requires_session_and_respects_ownership(
        self, auth_client: AsyncClient, auth_engine
    ):
        reg = await auth_client.post(
            "/api/auth/register",
            json={"email": "cases@example.com", "password": "secret123"},
        )
        token = reg.json()["session_token"]

        Session = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            u = (await db.execute(select(User).where(User.email == "cases@example.com"))).scalar_one()
            job = SearchJob(petitioner_name="X", year="2020", status="done", user_id=u.id)
            db.add(job)
            await db.flush()
            db.add(
                Case(
                    job_id=job.id,
                    sr_no="1",
                    case_type_number_year="TST/1/2020",
                    petitioner_vs_respondent="A v B",
                )
            )
            await db.commit()
            await db.refresh(job)
            job_id = job.id

        async with AsyncClient(transport=auth_client._transport, base_url="http://test") as anon:
            assert (await anon.get(f"/api/jobs/{job_id}/cases")).status_code == 401

        async with _bearer_client(auth_client._transport, token) as bearer:
            listed = await bearer.get(f"/api/jobs/{job_id}/cases")
            assert listed.status_code == 200
            assert listed.json()["total"] == 1
