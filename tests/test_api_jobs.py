"""
Integration tests for the jobs API routes.
  GET  /api/health
  POST /api/jobs
  GET  /api/jobs/{job_id}
  GET  /api/jobs
"""

import pytest
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient

from tests.conftest import make_job


# ── Health ────────────────────────────────────────────────────────────────────

class TestHealth:

    async def test_health_returns_ok(self, client: AsyncClient):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_health_no_auth_required(self, client: AsyncClient):
        resp = await client.get("/api/health")
        assert resp.status_code == 200


# ── POST /api/jobs ────────────────────────────────────────────────────────────

class TestCreateJob:

    async def test_create_job_returns_202(self, client: AsyncClient):
        resp = await client.post("/api/jobs", json={"petitioner_name": "Rajesh Gupta"})
        assert resp.status_code == 202

    async def test_create_job_response_has_job_id(self, client: AsyncClient):
        resp = await client.post("/api/jobs", json={"petitioner_name": "Rajesh Gupta"})
        body = resp.json()
        assert "job_id" in body
        assert len(body["job_id"]) == 36  # UUID format

    async def test_create_job_initial_status_is_pending(self, client: AsyncClient):
        resp = await client.post("/api/jobs", json={"petitioner_name": "Test Person"})
        assert resp.json()["status"] == "pending"

    async def test_create_job_with_year(self, client: AsyncClient):
        resp = await client.post(
            "/api/jobs", json={"petitioner_name": "Rajesh Gupta", "year": "2019"}
        )
        assert resp.status_code == 202
        assert resp.json()["year"] == "2019"

    async def test_create_job_without_year_is_none(self, client: AsyncClient):
        resp = await client.post("/api/jobs", json={"petitioner_name": "Rajesh Gupta"})
        assert resp.json()["year"] is None

    async def test_create_job_name_too_short_returns_422(self, client: AsyncClient):
        resp = await client.post("/api/jobs", json={"petitioner_name": "AB"})
        assert resp.status_code == 422

    async def test_create_job_invalid_year_returns_422(self, client: AsyncClient):
        resp = await client.post(
            "/api/jobs", json={"petitioner_name": "Valid Name", "year": "abc"}
        )
        assert resp.status_code == 422

    async def test_create_job_year_too_short_returns_422(self, client: AsyncClient):
        resp = await client.post(
            "/api/jobs", json={"petitioner_name": "Valid Name", "year": "20"}
        )
        assert resp.status_code == 422

    async def test_create_job_missing_name_returns_422(self, client: AsyncClient):
        resp = await client.post("/api/jobs", json={})
        assert resp.status_code == 422

    async def test_create_job_progress_pct_starts_at_zero(self, client: AsyncClient):
        resp = await client.post("/api/jobs", json={"petitioner_name": "Test Person"})
        assert resp.json()["progress_pct"] == 0

    async def test_create_job_total_cases_starts_at_zero(self, client: AsyncClient):
        resp = await client.post("/api/jobs", json={"petitioner_name": "Test Person"})
        assert resp.json()["total_cases"] == 0


# ── GET /api/jobs/{job_id} ────────────────────────────────────────────────────

class TestGetJob:

    async def test_get_existing_job(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        resp = await client.get(f"/api/jobs/{job.id}")
        assert resp.status_code == 200
        assert resp.json()["job_id"] == job.id

    async def test_get_job_returns_correct_name(self, client: AsyncClient, db_session):
        job = await make_job(db_session, name="Sunita Sharma")
        resp = await client.get(f"/api/jobs/{job.id}")
        assert resp.json()["petitioner_name"] == "Sunita Sharma"

    async def test_get_job_returns_correct_year(self, client: AsyncClient, db_session):
        job = await make_job(db_session, year="2020")
        resp = await client.get(f"/api/jobs/{job.id}")
        assert resp.json()["year"] == "2020"

    async def test_get_job_not_found_returns_404(self, client: AsyncClient):
        resp = await client.get("/api/jobs/non-existent-uuid")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    async def test_get_job_done_status(self, client: AsyncClient, db_session):
        job = await make_job(db_session, status="done")
        resp = await client.get(f"/api/jobs/{job.id}")
        assert resp.json()["status"] == "done"

    async def test_get_job_failed_status(self, client: AsyncClient, db_session):
        job = await make_job(db_session, status="failed")
        resp = await client.get(f"/api/jobs/{job.id}")
        assert resp.json()["status"] == "failed"

    async def test_get_job_contains_progress_pct_field(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        resp = await client.get(f"/api/jobs/{job.id}")
        assert "progress_pct" in resp.json()


# ── GET /api/jobs ─────────────────────────────────────────────────────────────

class TestListJobs:

    async def test_list_jobs_empty(self, client: AsyncClient):
        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert "jobs" in body
        assert "total" in body

    async def test_list_jobs_returns_created_job(self, client: AsyncClient, db_session):
        job = await make_job(db_session, name="List Test Person")
        resp = await client.get("/api/jobs")
        ids = [j["job_id"] for j in resp.json()["jobs"]]
        assert job.id in ids

    async def test_list_jobs_total_reflects_count(self, client: AsyncClient, db_session):
        before = (await client.get("/api/jobs")).json()["total"]
        await make_job(db_session, name="Extra Job 1")
        await make_job(db_session, name="Extra Job 2")
        resp = await client.get("/api/jobs")
        assert resp.json()["total"] >= before + 2

    async def test_list_jobs_newest_first(self, client: AsyncClient, db_session):
        j1 = await make_job(db_session, name="Older Job")
        j2 = await make_job(db_session, name="Newer Job")
        resp = await client.get("/api/jobs?limit=50")
        ids = [j["job_id"] for j in resp.json()["jobs"]]
        # j2 was created after j1, should appear first
        assert ids.index(j2.id) < ids.index(j1.id)

    async def test_list_jobs_limit_respected(self, client: AsyncClient, db_session):
        for i in range(5):
            await make_job(db_session, name=f"Pagination Person {i}")
        resp = await client.get("/api/jobs?limit=2")
        assert len(resp.json()["jobs"]) <= 2

    async def test_list_jobs_offset_skips_records(self, client: AsyncClient, db_session):
        for i in range(4):
            await make_job(db_session, name=f"Offset Person {i}")
        resp_all = await client.get("/api/jobs?limit=100")
        resp_offset = await client.get("/api/jobs?limit=100&offset=2")
        assert len(resp_offset.json()["jobs"]) <= len(resp_all.json()["jobs"]) - 2
