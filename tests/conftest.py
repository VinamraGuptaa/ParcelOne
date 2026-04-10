"""
Shared pytest fixtures for the eCourts scraper test suite.

Uses an in-memory SQLite database (StaticPool) so every test run
is isolated and no files are left on disk.
"""

import os

# Must be set before any app module is imported so database.py
# picks up the test URL at module-load time.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from api.database import Base, get_db
from api.app import create_app
from api.models import SearchJob, Case


# ── Test engine (shared across the whole session) ──────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def engine():
    """Single in-memory SQLite engine reused for the whole test session."""
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
async def db_session(engine):
    """
    Per-test async DB session.
    Rolls back after each test so tests are isolated.
    """
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(engine):
    """
    HTTPX async client wired to the FastAPI app.
    get_db is overridden to use the test engine's session factory.
    asyncio.create_task(run_scrape_job(...)) is patched to a no-op so
    tests don't accidentally launch a real Playwright browser.
    """
    from unittest.mock import patch, AsyncMock

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    # Prevent any real scraping during API tests
    with patch("api.routes.jobs.run_scrape_job", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac


# ── Convenience helpers ─────────────────────────────────────────────────────

async def make_job(session: AsyncSession, name="Rajesh Gupta", year="2017", status="done") -> SearchJob:
    """Insert a SearchJob row and return the ORM object."""
    job = SearchJob(petitioner_name=name, year=year, status=status)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def make_case(session: AsyncSession, job_id: str, **kwargs) -> Case:
    """Insert a Case row linked to job_id and return it."""
    defaults = dict(
        sr_no="1",
        case_type_number_year="R.C.A./181/2017",
        petitioner_vs_respondent="Asha Rajesh Gupta",
        cnr_number="MHPU010023222017",
        case_type="R.C.A. - Regular Civil Appeal",
        filing_number="1252/2017",
        filing_date="16-02-2017",
        registration_number="181/2017",
        registration_date="21-03-2017",
        search_year="2017",
    )
    defaults.update(kwargs)
    case = Case(job_id=job_id, **defaults)
    session.add(case)
    await session.commit()
    await session.refresh(case)
    return case
