"""
Background scrape job runner.

Each call to run_scrape_job() is submitted via asyncio.create_task() from
the POST /api/jobs route. It owns the full Playwright scraper lifecycle and
writes progress updates directly to the database.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select, delete

from api.database import AsyncSessionLocal
from api.models import SearchJob, Case

# How many completed jobs to retain; older ones (+ their cases) are deleted.
MAX_JOBS_RETAINED = int(os.getenv("MAX_JOBS", "10"))

# Per-year timeout tuning knobs
_OVERHEAD_PER_YEAR_SECS = 60    # navigation + CAPTCHA + rate-limit delays
_SECS_PER_RECORD = 15           # detail fetch (~3s) + rate-limit delay (~8s) + buffer
_DEFAULT_RECORD_ESTIMATE = 10   # assumed record count for first year (no history yet)
_MIN_YEAR_TIMEOUT_SECS = 120    # floor — even a 0-record year needs time for navigation

# Hard override: set SCRAPE_TIMEOUT_SECONDS in .env to cap every year's budget.
_TIMEOUT_OVERRIDE = os.getenv("SCRAPE_TIMEOUT_SECONDS")


def _calc_year_timeout(estimated_records: int) -> int:
    """Timeout for a single year's scrape, based on expected record count.

    Uses observed timings: ~60s overhead + ~15s/record (detail fetch + delay).
    Applies a 1.5× buffer on the record estimate to absorb variance.

    Examples (with default estimate=10 for year 1):
        est 0  records →  120s (2 min, floor)
        est 10 records →  285s (4.8 min)
        est 20 records →  510s (8.5 min)
        est 50 records → 1185s (19.8 min)

    Override all dynamic logic by setting SCRAPE_TIMEOUT_SECONDS in .env.
    """
    if _TIMEOUT_OVERRIDE:
        return int(_TIMEOUT_OVERRIDE)
    buffered = int(estimated_records * 1.5)
    return max(_MIN_YEAR_TIMEOUT_SECS, _OVERHEAD_PER_YEAR_SECS + buffered * _SECS_PER_RECORD)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _last_15_years() -> list[str]:
    import datetime as dt
    current_year = dt.datetime.now().year
    return [str(y) for y in range(current_year, current_year - 15, -1)]


async def run_scrape_job(job_id: str) -> None:
    """
    Run a full scrape job for the given job_id.

    Lifecycle:
      1. Mark job as running
      2. Launch Playwright scraper (always headless)
      3. Iterate over years, calling search_petitioner() per year
      4. Insert Case rows after each year
      5. Mark job as done (or failed on exception)
    """
    logger.info(f"==> Worker task started for job {job_id}")

    # Import here to avoid circular imports at module load time
    from scraper import ECourtsScraper

    scraper = ECourtsScraper(headless=True)
    logger.info(f"ECourtsScraper instantiated (headless=True)")

    async with AsyncSessionLocal() as db:
        # Load the job
        result = await db.execute(select(SearchJob).where(SearchJob.id == job_id))
        job: SearchJob | None = result.scalar_one_or_none()
        if job is None:
            logger.error(f"Job {job_id} not found in DB.")
            return

        job.status = "running"
        job.started_at = _now()
        await db.commit()

    try:
        await scraper.setup_driver()
        await scraper.navigate_and_select()

        # Determine years to scrape
        if job.year:
            years = [job.year]
        else:
            years = _last_15_years()

        # Update years_total
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(SearchJob).where(SearchJob.id == job_id))
            job = result.scalar_one()
            job.years_total = len(years)
            job.progress_message = f"Starting scrape for {len(years)} year(s)..."
            await db.commit()

        total_cases = 0
        total_records_seen = 0   # running total used to calibrate per-year timeouts
        years_completed = 0      # years with known record counts

        for i, year in enumerate(years):
            progress_msg = f"Scraping year {year} ({i + 1}/{len(years)})..."
            logger.info(progress_msg)

            # Estimate records for this year using running average from prior years.
            # Falls back to _DEFAULT_RECORD_ESTIMATE for the first year.
            if years_completed > 0:
                est_records = int(total_records_seen / years_completed)
            else:
                est_records = _DEFAULT_RECORD_ESTIMATE

            year_timeout = _calc_year_timeout(est_records)
            logger.info(
                f"Year {year} timeout: {year_timeout}s "
                f"(est. {est_records} records, "
                f"{int(est_records * 1.5)} with 1.5× buffer)"
            )

            # Update progress
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(SearchJob).where(SearchJob.id == job_id)
                )
                job = result.scalar_one()
                job.progress_message = progress_msg
                await db.commit()

            try:
                async with asyncio.timeout(year_timeout):
                    # Re-navigate for every year after the first
                    if i > 0:
                        await scraper.navigate_and_select()
                        await asyncio.sleep(random_delay())

                    records = await scraper.search_petitioner(job.petitioner_name, year)

            except TimeoutError:
                mins, secs = year_timeout // 60, year_timeout % 60
                msg = (
                    f"Year {year} timed out after {mins}m {secs}s "
                    f"(estimated {est_records} records). "
                    "The site may be slow or unresponsive."
                )
                logger.error(msg)
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(SearchJob).where(SearchJob.id == job_id))
                    job = result.scalar_one_or_none()
                    if job:
                        job.status = "failed"
                        job.error_message = msg
                        job.finished_at = _now()
                        await db.commit()
                return  # exit run_scrape_job; finally still runs scraper.close()

            # Tag each record with the year and persist to DB
            if records:
                async with AsyncSessionLocal() as db:
                    for rec in records:
                        rec["Search_Year"] = year
                        case = Case(
                            job_id=job_id,
                            search_year=year,
                            sr_no=rec.get("Sr No") or rec.get("col_0"),
                            case_type_number_year=rec.get("Case Type/Case Number/Case Year") or rec.get("col_1"),
                            petitioner_vs_respondent=rec.get("Petitioner Name versus Respondent Name") or rec.get("col_2"),
                            cnr_number=rec.get("CNR_Number"),
                            case_type=rec.get("Case_Type"),
                            filing_number=rec.get("Filing_Number"),
                            filing_date=rec.get("Filing_Date"),
                            registration_number=rec.get("Registration_Number"),
                            registration_date=rec.get("Registration_Date"),
                            efiling_number=rec.get("eFiling_Number"),
                            efiling_date=rec.get("eFiling_Date"),
                            under_acts=rec.get("Under_Acts"),
                            first_hearing_date=rec.get("First_Hearing_Date"),
                            next_hearing_date=rec.get("Next_Hearing_Date"),
                            case_stage=rec.get("Case_Stage"),
                            decision_date=rec.get("Decision_Date"),
                            case_status=rec.get("Case_Status"),
                            nature_of_disposal=rec.get("Nature_of_Disposal"),
                            court_number_judge=rec.get("Court_Number_Judge"),
                            petitioner_and_advocate=rec.get("Petitioner_and_Advocate"),
                            respondent_and_advocate=rec.get("Respondent_and_Advocate"),
                            raw_json=json.dumps(rec, ensure_ascii=False),
                        )
                        db.add(case)
                    await db.commit()

                total_cases += len(records)

            # Calibrate: update running record average with this year's actual count
            total_records_seen += len(records)
            years_completed += 1

            # Update years_done
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(SearchJob).where(SearchJob.id == job_id)
                )
                job = result.scalar_one()
                job.years_done = i + 1
                job.total_cases = total_cases
                await db.commit()

        # Mark done
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(SearchJob).where(SearchJob.id == job_id))
            job = result.scalar_one()
            job.status = "done"
            job.finished_at = _now()
            job.total_cases = total_cases
            job.progress_message = f"Completed. Found {total_cases} case(s)."
            await db.commit()

        logger.info(f"Job {job_id} completed: {total_cases} total cases.")
        await _cleanup_old_jobs()

    except Exception as e:
        logger.exception(f"Job {job_id} failed: {e}")
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(SearchJob).where(SearchJob.id == job_id))
            job = result.scalar_one_or_none()
            if job:
                job.status = "failed"
                job.error_message = str(e)
                job.finished_at = _now()
                await db.commit()

    finally:
        await scraper.close()


async def _cleanup_old_jobs() -> None:
    """
    Retain only the MAX_JOBS_RETAINED most recent jobs.
    Deletes older SearchJob rows; Case rows are removed via cascade.
    """
    async with AsyncSessionLocal() as db:
        # IDs of the N most recent jobs (any status)
        keep_result = await db.execute(
            select(SearchJob.id)
            .order_by(SearchJob.created_at.desc())
            .limit(MAX_JOBS_RETAINED)
        )
        keep_ids = {row[0] for row in keep_result.fetchall()}

        # Find jobs outside the retention window
        old_result = await db.execute(
            select(SearchJob).where(SearchJob.id.not_in(keep_ids))
        )
        old_jobs = old_result.scalars().all()

        if not old_jobs:
            return

        for job in old_jobs:
            await db.delete(job)  # cascade deletes associated Case rows

        await db.commit()
        logger.info(
            f"Cleanup: deleted {len(old_jobs)} old job(s) "
            f"(retaining last {MAX_JOBS_RETAINED})."
        )


def random_delay() -> float:
    import random
    return random.uniform(3, 7)
