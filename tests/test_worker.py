"""
Unit tests for api/worker.py — background scrape job lifecycle.

The ECourtsScraper and AsyncSessionLocal are both mocked so no
browser or real database session is touched.
"""

import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from api.database import Base
from api.models import SearchJob, Case


# ── Test DB setup (separate from the shared conftest engine) ──────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


async def _make_test_session_factory():
    eng = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False), eng


def _make_scraper_mock(records: list[dict] | None = None):
    """Return a fully mocked ECourtsScraper instance."""
    mock = MagicMock()
    mock.setup_driver = AsyncMock()
    mock.navigate_and_select = AsyncMock()
    mock.search_petitioner = AsyncMock(return_value=records or [])
    mock.close = AsyncMock()
    return mock


SAMPLE_RECORDS = [
    {
        "Sr No": "1",
        "Case Type/Case Number/Case Year": "R.C.A./181/2017",
        "Petitioner Name versus Respondent Name": "Asha Rajesh Gupta",
        "CNR_Number": "MHPU010023222017",
        "Case_Type": "R.C.A. - Regular Civil Appeal",
        "Filing_Number": "1252/2017",
        "Filing_Date": "16-02-2017",
        "Registration_Number": "181/2017",
        "Registration_Date": "21-03-2017",
    },
    {
        "Sr No": "2",
        "Case Type/Case Number/Case Year": "Civil M.A./465/2017",
        "Petitioner Name versus Respondent Name": "Vipin Gupta Vs Rajesh Gupta",
        "CNR_Number": "MHPU020028962017",
        "Case_Type": "Civil M.A. - Civil Misc. Application",
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _create_job(session_factory, name="Rajesh Gupta", year="2017") -> SearchJob:
    async with session_factory() as session:
        job = SearchJob(petitioner_name=name, year=year, status="pending")
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job


async def _get_job(session_factory, job_id: str) -> SearchJob | None:
    from sqlalchemy import select
    async with session_factory() as session:
        result = await session.execute(select(SearchJob).where(SearchJob.id == job_id))
        return result.scalar_one_or_none()


async def _get_cases(session_factory, job_id: str) -> list[Case]:
    from sqlalchemy import select
    async with session_factory() as session:
        result = await session.execute(select(Case).where(Case.job_id == job_id))
        return result.scalars().all()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunScrapeJob:

    async def test_job_not_found_exits_gracefully(self):
        """run_scrape_job with a non-existent ID should not raise."""
        session_factory, eng = await _make_test_session_factory()
        scraper_mock = _make_scraper_mock()

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            # Should not raise
            await run_scrape_job("non-existent-uuid")

        scraper_mock.setup_driver.assert_not_called()
        await eng.dispose()

    async def test_job_marked_running_at_start(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory)
        scraper_mock = _make_scraper_mock(records=[])

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        final_job = await _get_job(session_factory, job.id)
        assert final_job.started_at is not None
        await eng.dispose()

    async def test_job_marked_done_on_success(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory)
        scraper_mock = _make_scraper_mock(records=[])

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        final_job = await _get_job(session_factory, job.id)
        assert final_job.status == "done"
        assert final_job.finished_at is not None
        await eng.dispose()

    async def test_job_marked_failed_on_scraper_exception(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory)

        scraper_mock = _make_scraper_mock()
        scraper_mock.setup_driver = AsyncMock(side_effect=RuntimeError("browser crashed"))

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        final_job = await _get_job(session_factory, job.id)
        assert final_job.status == "failed"
        assert "browser crashed" in final_job.error_message
        await eng.dispose()

    async def test_cases_inserted_for_found_records(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory, year="2017")
        scraper_mock = _make_scraper_mock(records=SAMPLE_RECORDS)

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        cases = await _get_cases(session_factory, job.id)
        assert len(cases) == 2
        await eng.dispose()

    async def test_total_cases_updated_on_job(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory, year="2017")
        scraper_mock = _make_scraper_mock(records=SAMPLE_RECORDS)

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        final_job = await _get_job(session_factory, job.id)
        assert final_job.total_cases == 2
        await eng.dispose()

    async def test_cnr_number_persisted_in_case(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory, year="2017")
        scraper_mock = _make_scraper_mock(records=SAMPLE_RECORDS)

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        cases = await _get_cases(session_factory, job.id)
        cnrs = [c.cnr_number for c in cases]
        assert "MHPU010023222017" in cnrs
        await eng.dispose()

    async def test_raw_json_stored_per_case(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory, year="2017")
        scraper_mock = _make_scraper_mock(records=SAMPLE_RECORDS)

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        cases = await _get_cases(session_factory, job.id)
        for case in cases:
            assert case.raw_json is not None
            parsed = json.loads(case.raw_json)
            assert "CNR_Number" in parsed or "Case_Type" in parsed
        await eng.dispose()

    async def test_scraper_close_called_even_on_failure(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory)
        scraper_mock = _make_scraper_mock()
        scraper_mock.navigate_and_select = AsyncMock(side_effect=Exception("nav error"))

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        scraper_mock.close.assert_called_once()
        await eng.dispose()

    async def test_years_total_set_for_single_year_job(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory, year="2019")
        scraper_mock = _make_scraper_mock(records=[])

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        final_job = await _get_job(session_factory, job.id)
        assert final_job.years_total == 1
        await eng.dispose()

    async def test_multi_year_job_iterates_all_years(self):
        """When no year is set, scraper is called once per year in the range."""
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory, year=None)
        scraper_mock = _make_scraper_mock(records=[])

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        # search_petitioner should have been called once per year
        call_count = scraper_mock.search_petitioner.call_count
        assert call_count >= 10   # at least 10 years
        await eng.dispose()

    async def test_search_year_tagged_on_each_case(self):
        session_factory, eng = await _make_test_session_factory()
        job = await _create_job(session_factory, year="2020")
        scraper_mock = _make_scraper_mock(records=SAMPLE_RECORDS[:1])

        with patch("api.worker.AsyncSessionLocal", session_factory), \
             patch("scraper.HybridECourtsScraper", return_value=scraper_mock):
            from api.worker import run_scrape_job
            await run_scrape_job(job.id)

        cases = await _get_cases(session_factory, job.id)
        assert all(c.search_year == "2020" for c in cases)
        await eng.dispose()
