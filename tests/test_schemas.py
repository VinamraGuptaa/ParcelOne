"""
Unit tests for Pydantic schemas (api/schemas.py).
No database or network I/O — pure validation logic.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from api.schemas import JobCreateRequest, JobResponse


# ── JobCreateRequest ─────────────────────────────────────────────────────────

class TestJobCreateRequest:

    def test_valid_name_and_no_year(self):
        req = JobCreateRequest(petitioner_name="Rajesh Gupta")
        assert req.petitioner_name == "Rajesh Gupta"
        assert req.year is None

    def test_valid_name_with_year(self):
        req = JobCreateRequest(petitioner_name="Rajesh Gupta", year="2017")
        assert req.year == "2017"

    def test_name_is_stripped(self):
        req = JobCreateRequest(petitioner_name="  Rajesh Gupta  ")
        assert req.petitioner_name == "Rajesh Gupta"

    def test_name_exactly_three_chars(self):
        req = JobCreateRequest(petitioner_name="Ram")
        assert req.petitioner_name == "Ram"

    def test_name_too_short_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            JobCreateRequest(petitioner_name="AB")
        assert "3 characters" in str(exc_info.value)

    def test_name_whitespace_only_raises(self):
        with pytest.raises(ValidationError):
            JobCreateRequest(petitioner_name="   ")

    def test_year_valid_four_digits(self):
        req = JobCreateRequest(petitioner_name="Test Name", year="2019")
        assert req.year == "2019"

    def test_year_empty_string_becomes_none(self):
        req = JobCreateRequest(petitioner_name="Test Name", year="")
        assert req.year is None

    def test_year_whitespace_becomes_none(self):
        req = JobCreateRequest(petitioner_name="Test Name", year="   ")
        assert req.year is None

    def test_year_non_digits_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            JobCreateRequest(petitioner_name="Test Name", year="abcd")
        assert "4-digit" in str(exc_info.value)

    def test_year_too_short_raises(self):
        with pytest.raises(ValidationError):
            JobCreateRequest(petitioner_name="Test Name", year="17")

    def test_year_too_long_raises(self):
        with pytest.raises(ValidationError):
            JobCreateRequest(petitioner_name="Test Name", year="20171")

    def test_year_with_spaces_stripped_then_validated(self):
        req = JobCreateRequest(petitioner_name="Test Name", year=" 2020 ")
        assert req.year == "2020"

    def test_year_none_is_accepted(self):
        req = JobCreateRequest(petitioner_name="Test Name", year=None)
        assert req.year is None


# ── JobResponse ──────────────────────────────────────────────────────────────

def _make_orm_stub(**overrides):
    """Return a SimpleNamespace that looks like a SearchJob ORM object."""
    from types import SimpleNamespace
    defaults = dict(
        id="job-uuid-1234",
        petitioner_name="Rajesh Gupta",
        year="2017",
        status="done",
        progress_message="Completed.",
        years_total=10,
        years_done=10,
        total_cases=8,
        error_message=None,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        finished_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestJobResponse:

    def test_from_orm_obj_maps_id_to_job_id(self):
        stub = _make_orm_stub()
        resp = JobResponse.from_orm_obj(stub)
        assert resp.job_id == "job-uuid-1234"

    def test_from_orm_obj_all_fields_present(self):
        stub = _make_orm_stub()
        resp = JobResponse.from_orm_obj(stub)
        assert resp.petitioner_name == "Rajesh Gupta"
        assert resp.year == "2017"
        assert resp.status == "done"
        assert resp.total_cases == 8

    def test_progress_pct_100_when_complete(self):
        stub = _make_orm_stub(years_done=10, years_total=10)
        resp = JobResponse.from_orm_obj(stub)
        assert resp.progress_pct == 100

    def test_progress_pct_partial(self):
        stub = _make_orm_stub(years_done=3, years_total=10)
        resp = JobResponse.from_orm_obj(stub)
        assert resp.progress_pct == 30

    def test_progress_pct_zero_when_not_started(self):
        stub = _make_orm_stub(years_done=0, years_total=None)
        resp = JobResponse.from_orm_obj(stub)
        assert resp.progress_pct == 0

    def test_progress_pct_zero_when_years_total_is_zero(self):
        stub = _make_orm_stub(years_done=0, years_total=0)
        resp = JobResponse.from_orm_obj(stub)
        assert resp.progress_pct == 0

    def test_progress_pct_rounds_down(self):
        # 1/3 = 33.33 → 33
        stub = _make_orm_stub(years_done=1, years_total=3)
        resp = JobResponse.from_orm_obj(stub)
        assert resp.progress_pct == 33

    def test_optional_fields_can_be_none(self):
        stub = _make_orm_stub(year=None, error_message=None, started_at=None, finished_at=None)
        resp = JobResponse.from_orm_obj(stub)
        assert resp.year is None
        assert resp.error_message is None
