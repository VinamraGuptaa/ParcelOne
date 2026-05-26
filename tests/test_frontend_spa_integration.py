"""Integration tests: FastAPI serves the React SPA and API routes together."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.app import create_app
from api.database import get_db


ROOT = Path(__file__).resolve().parents[1]
REACT_DIST = ROOT / "frontend" / "dist"


@pytest.fixture
def spa_available() -> bool:
    return (REACT_DIST / "index.html").is_file()


@pytest.fixture
async def spa_client(spa_available, engine):
    if not spa_available:
        pytest.skip("frontend/dist not built — run: cd frontend && npm run build")

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_api_routes_take_priority_over_spa(spa_client: AsyncClient):
    """API paths must not be swallowed by the SPA catch-all."""
    health = await spa_client.get("/api/health")
    assert health.status_code == 200
    assert health.json().get("status") in ("ok", "degraded")

    with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock):
        wf = await spa_client.get("/api/workflows")
    assert wf.status_code == 200
    assert "workflows" in wf.json()


@pytest.mark.asyncio
async def test_spa_index_served_at_root(spa_client: AsyncClient):
    resp = await spa_client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="root"' in html


@pytest.mark.asyncio
async def test_spa_deep_link_serves_index_html(spa_client: AsyncClient):
    """React Router paths must return index.html (AWS same-origin deploy)."""
    resp = await spa_client.get("/report/workflow/test-id")
    assert resp.status_code == 200
    assert 'id="root"' in resp.text


@pytest.mark.asyncio
async def test_spa_index_html_path(spa_client: AsyncClient):
    resp = await spa_client.get("/index.html")
    assert resp.status_code == 200
    assert 'id="root"' in resp.text


@pytest.mark.asyncio
async def test_bhulekh_catalog_served_from_dist(spa_client: AsyncClient):
    resp = await spa_client.get("/data/bhulekh_catalog.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "districts" in data


@pytest.mark.asyncio
async def test_spa_and_cors_together(spa_client: AsyncClient):
    origin = "https://icy-disk.example.com"
    resp = await spa_client.get(
        "/api/workflows",
        headers={"Origin": origin},
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") is not None
