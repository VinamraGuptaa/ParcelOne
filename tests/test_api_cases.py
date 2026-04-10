"""
Integration tests for the cases API routes.
  GET  /api/jobs/{job_id}/cases
  GET  /api/jobs/{job_id}/cases/export
"""

import csv
import io
import pytest
from httpx import AsyncClient

from tests.conftest import make_job, make_case


# ── GET /api/jobs/{job_id}/cases ──────────────────────────────────────────────

class TestListCases:

    async def test_list_cases_unknown_job_returns_404(self, client: AsyncClient):
        resp = await client.get("/api/jobs/nonexistent-id/cases")
        assert resp.status_code == 404

    async def test_list_cases_empty_when_no_cases(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        resp = await client.get(f"/api/jobs/{job.id}/cases")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cases"] == []
        assert body["total"] == 0
        assert body["job_id"] == job.id

    async def test_list_cases_returns_inserted_case(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        case = await make_case(db_session, job.id)
        resp = await client.get(f"/api/jobs/{job.id}/cases")
        assert resp.status_code == 200
        cases = resp.json()["cases"]
        assert len(cases) == 1
        assert cases[0]["cnr_number"] == case.cnr_number

    async def test_list_cases_total_matches_inserted_count(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        for i in range(3):
            await make_case(db_session, job.id, cnr_number=f"CNR{i:04d}", sr_no=str(i + 1))
        resp = await client.get(f"/api/jobs/{job.id}/cases")
        assert resp.json()["total"] == 3

    async def test_list_cases_all_expected_fields_present(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        await make_case(db_session, job.id)
        resp = await client.get(f"/api/jobs/{job.id}/cases")
        case = resp.json()["cases"][0]
        for field in [
            "id", "job_id", "search_year", "sr_no", "case_type_number_year",
            "petitioner_vs_respondent", "cnr_number", "case_type",
            "filing_number", "filing_date", "created_at",
        ]:
            assert field in case, f"Missing field: {field}"

    async def test_list_cases_limit_respected(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        for i in range(5):
            await make_case(db_session, job.id, cnr_number=f"LIMIT{i:04d}", sr_no=str(i + 1))
        resp = await client.get(f"/api/jobs/{job.id}/cases?limit=2")
        assert len(resp.json()["cases"]) == 2

    async def test_list_cases_offset_skips_records(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        for i in range(4):
            await make_case(db_session, job.id, cnr_number=f"OFFS{i:04d}", sr_no=str(i + 1))
        resp_all = await client.get(f"/api/jobs/{job.id}/cases?limit=100")
        resp_offset = await client.get(f"/api/jobs/{job.id}/cases?limit=100&offset=2")
        all_ids = [c["id"] for c in resp_all.json()["cases"]]
        offset_ids = [c["id"] for c in resp_offset.json()["cases"]]
        assert len(offset_ids) == 2
        assert offset_ids == all_ids[2:]

    async def test_list_cases_ordered_by_id(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        for i in range(3):
            await make_case(db_session, job.id, cnr_number=f"ORD{i:04d}", sr_no=str(i + 1))
        resp = await client.get(f"/api/jobs/{job.id}/cases")
        ids = [c["id"] for c in resp.json()["cases"]]
        assert ids == sorted(ids)

    async def test_list_cases_only_returns_cases_for_that_job(self, client: AsyncClient, db_session):
        job_a = await make_job(db_session, name="Person A")
        job_b = await make_job(db_session, name="Person B")
        await make_case(db_session, job_a.id, cnr_number="JOBA0001")
        await make_case(db_session, job_b.id, cnr_number="JOBB0001")
        resp = await client.get(f"/api/jobs/{job_a.id}/cases")
        cases = resp.json()["cases"]
        assert all(c["job_id"] == job_a.id for c in cases)


# ── GET /api/jobs/{job_id}/cases/export ──────────────────────────────────────

class TestExportCases:

    async def test_export_unknown_job_returns_404(self, client: AsyncClient):
        resp = await client.get("/api/jobs/nonexistent-id/cases/export")
        assert resp.status_code == 404

    async def test_export_no_cases_returns_404(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        resp = await client.get(f"/api/jobs/{job.id}/cases/export")
        assert resp.status_code == 404

    async def test_export_returns_csv_content_type(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        await make_case(db_session, job.id)
        resp = await client.get(f"/api/jobs/{job.id}/cases/export")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    async def test_export_has_content_disposition_attachment(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        await make_case(db_session, job.id)
        resp = await client.get(f"/api/jobs/{job.id}/cases/export")
        assert "attachment" in resp.headers["content-disposition"]
        assert ".csv" in resp.headers["content-disposition"]

    async def test_export_csv_has_header_row(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        await make_case(db_session, job.id)
        resp = await client.get(f"/api/jobs/{job.id}/cases/export")
        reader = csv.DictReader(io.StringIO(resp.text))
        assert reader.fieldnames is not None
        assert "cnr_number" in reader.fieldnames
        assert "case_type_number_year" in reader.fieldnames
        assert "petitioner_vs_respondent" in reader.fieldnames

    async def test_export_csv_data_matches_case(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        await make_case(db_session, job.id, cnr_number="TESTCNR001")
        resp = await client.get(f"/api/jobs/{job.id}/cases/export")
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["cnr_number"] == "TESTCNR001"

    async def test_export_csv_multiple_cases(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        for i in range(3):
            await make_case(db_session, job.id, cnr_number=f"CSV{i:04d}", sr_no=str(i + 1))
        resp = await client.get(f"/api/jobs/{job.id}/cases/export")
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
        assert len(rows) == 3

    async def test_export_filename_contains_job_id_prefix(self, client: AsyncClient, db_session):
        job = await make_job(db_session)
        await make_case(db_session, job.id)
        resp = await client.get(f"/api/jobs/{job.id}/cases/export")
        disposition = resp.headers["content-disposition"]
        assert job.id[:8] in disposition
