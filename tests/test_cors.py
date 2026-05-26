"""CORS middleware tests — ensure the API is callable from a separate frontend origin."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import api.routes.workflows as workflows_routes
from api.app import create_app
from api.database import get_db


@asynccontextmanager
async def _cors_client(monkeypatch, engine, origins: str):
    monkeypatch.setenv("CORS_ORIGINS", origins)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_cors_preflight_allows_configured_origin(monkeypatch, engine):
    origin = "https://icy-disk.example.com"
    async with _cors_client(monkeypatch, engine, origin) as client:
        resp = await client.options(
            "/api/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin
    assert "access-control-allow-methods" in resp.headers


@pytest.mark.asyncio
async def test_cors_get_includes_allow_origin_for_matching_origin(monkeypatch, engine):
    origin = "https://app.example.com"
    async with _cors_client(monkeypatch, engine, origin) as client:
        resp = await client.get("/api/health", headers={"Origin": origin})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin


@pytest.mark.asyncio
async def test_cors_wildcard_allows_any_origin(monkeypatch, engine):
    async with _cors_client(monkeypatch, engine, "*") as client:
        resp = await client.get(
            "/api/health",
            headers={"Origin": "https://random-frontend.example"},
        )
    assert resp.status_code == 200
    acao = resp.headers.get("access-control-allow-origin")
    assert acao in ("*", "https://random-frontend.example")


@pytest.mark.asyncio
async def test_cors_allows_post_to_workflows_with_origin_header(monkeypatch, engine):
    """Browser POST from SPA must pass CORS before land-case-search body is read."""
    origin = "http://localhost:5173"
    async with _cors_client(monkeypatch, engine, origin) as client:
        with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock), patch.object(
            workflows_routes,
            "ACTIVE_WORKFLOW_STATUSES",
            ("bhulekh_running", "igr_running"),
        ):
            preflight = await client.options(
                "/api/workflows/land-case-search",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert preflight.status_code == 200

            resp = await client.post(
                "/api/workflows/land-case-search",
                headers={"Origin": origin},
                json={
                    "village_label": "Wagholi",
                    "survey_part1": "70",
                    "survey_option_label": "70/6",
                },
            )
    assert resp.status_code == 202
    assert resp.headers.get("access-control-allow-origin") == origin


@pytest.mark.asyncio
async def test_cors_multiple_origins_comma_separated(monkeypatch, engine):
    origins = "https://app.example.com,https://icy-disk.example.com"
    monkeypatch.setenv("CORS_ORIGINS", origins)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for origin in ["https://app.example.com", "https://icy-disk.example.com"]:
            resp = await client.get("/api/health", headers={"Origin": origin})
            assert resp.status_code == 200
            assert resp.headers.get("access-control-allow-origin") == origin
