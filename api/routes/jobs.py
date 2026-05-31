"""Routes: POST /api/jobs, GET /api/jobs, GET /api/jobs/{job_id}"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import SearchJob, User
from api.auth import auth_enabled, get_current_user
from api.schemas import JobCreateRequest, JobResponse, JobListResponse
from api.worker import run_scrape_job

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = logging.getLogger(__name__)


@router.post("", status_code=202, response_model=JobResponse)
async def create_job(
    body: JobCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Submit a new scrape job. Returns 202 immediately; scraping runs in background."""
    job = SearchJob(
        petitioner_name=body.petitioner_name,
        year=body.year,
        user_id=current_user.id if current_user else None,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Fire-and-forget: submit to the asyncio event loop
    asyncio.create_task(run_scrape_job(str(job.id)))
    logger.info(f"Job {job.id} created for '{body.petitioner_name}' / year={body.year}")

    return JobResponse.from_orm_obj(job)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Poll a job's status and progress."""
    query = select(SearchJob).where(SearchJob.id == job_id)
    if auth_enabled() and current_user is not None:
        query = query.where(SearchJob.user_id == current_user.id)
    result = await db.execute(query)
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JobResponse.from_orm_obj(job)


@router.get("", response_model=JobListResponse)
async def list_jobs(
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """List all past jobs, newest first."""
    base_filter = select(SearchJob)
    count_filter = select(func.count()).select_from(SearchJob)
    if auth_enabled() and current_user is not None:
        base_filter = base_filter.where(SearchJob.user_id == current_user.id)
        count_filter = count_filter.where(SearchJob.user_id == current_user.id)

    count_result = await db.execute(count_filter)
    total = count_result.scalar_one()

    result = await db.execute(
        base_filter.order_by(SearchJob.created_at.desc()).limit(limit).offset(offset)
    )
    jobs = result.scalars().all()

    return JobListResponse(
        jobs=[JobResponse.from_orm_obj(j) for j in jobs],
        total=total,
    )
