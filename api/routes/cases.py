"""Routes: GET /api/jobs/{job_id}/cases, GET /api/jobs/{job_id}/cases/export"""

import csv
import io
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import auth_enabled, get_current_user
from api.database import get_db
from api.models import SearchJob, Case, User
from api.schemas import CaseResponse, CasesListResponse

router = APIRouter(tags=["cases"])
logger = logging.getLogger(__name__)


async def _get_job_for_user(job_id: str, db: AsyncSession, current_user: User | None) -> SearchJob:
    query = select(SearchJob).where(SearchJob.id == job_id)
    if auth_enabled() and current_user is not None:
        query = query.where(SearchJob.user_id == current_user.id)
    result = await db.execute(query)
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.get("/jobs/{job_id}/cases", response_model=CasesListResponse)
async def list_cases(
    job_id: str,
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Return all cases for a given job (paginated)."""
    await _get_job_for_user(job_id, db, current_user)

    count_result = await db.execute(
        select(func.count()).select_from(Case).where(Case.job_id == job_id)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Case)
        .where(Case.job_id == job_id)
        .order_by(Case.id)
        .limit(limit)
        .offset(offset)
    )
    cases = result.scalars().all()

    return CasesListResponse(
        job_id=job_id,
        cases=[CaseResponse.model_validate(c) for c in cases],
        total=total,
    )


@router.get("/jobs/{job_id}/cases/export")
async def export_cases(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Stream all cases for a job as a CSV download."""
    await _get_job_for_user(job_id, db, current_user)

    result = await db.execute(
        select(Case).where(Case.job_id == job_id).order_by(Case.id)
    )
    cases = result.scalars().all()

    if not cases:
        raise HTTPException(status_code=404, detail="No cases found for this job.")

    # Build CSV in memory
    output = io.StringIO()
    fieldnames = [
        "id", "job_id", "search_year", "sr_no", "case_type_number_year",
        "petitioner_vs_respondent", "cnr_number", "case_type",
        "filing_number", "filing_date", "registration_number", "registration_date",
        "efiling_number", "efiling_date", "under_acts", "first_hearing_date",
        "next_hearing_date", "case_stage", "decision_date", "case_status",
        "nature_of_disposal", "court_number_judge",
        "petitioner_and_advocate", "respondent_and_advocate", "created_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for case in cases:
        writer.writerow({f: getattr(case, f, "") for f in fieldnames})

    output.seek(0)
    filename = f"cases_{job_id[:8]}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
