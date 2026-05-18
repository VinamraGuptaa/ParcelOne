"""FastAPI application factory."""

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from api.database import engine, Base, AsyncSessionLocal, ensure_workflow_igr_hit_columns
from api.models import SearchJob, LandCaseWorkflow
from api.routes.jobs import router as jobs_router
from api.routes.cases import router as cases_router
from api.routes.workflows import router as workflows_router

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="eCourts India Case Scraper API",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
    )

    # CORS — allow the Netlify/Vercel frontend origin
    origins_env = os.getenv("CORS_ORIGINS", "*")
    origins = [o.strip() for o in origins_env.split(",") if o.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Create DB tables on startup + recover orphaned jobs
    @app.on_event("startup")
    async def _startup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Backward-compatible column migration for existing deployments.
            try:
                await conn.execute(
                    text("ALTER TABLE land_case_workflows ADD COLUMN owner_name_input TEXT")
                )
                logger.info("Applied DB migration: land_case_workflows.owner_name_input")
            except Exception as exc:
                msg = str(exc).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    logger.info("DB migration already applied: land_case_workflows.owner_name_input")
                else:
                    logger.warning("DB migration skipped for owner_name_input: %s", exc)
            ecourts_case_column_migrations = [
                ("case_type_raw", "TEXT"),
                ("court_no", "TEXT"),
                ("district", "TEXT"),
                ("state", "TEXT"),
                ("case_number", "TEXT"),
                ("cnr_year", "TEXT"),
                ("filing_number", "TEXT"),
                ("filing_date", "TEXT"),
                ("registration_number", "TEXT"),
                ("registration_date", "TEXT"),
                ("first_hearing_date", "TEXT"),
                ("next_hearing_date", "TEXT"),
                ("decision_date", "TEXT"),
                ("petitioners_json", "TEXT"),
                ("respondents_json", "TEXT"),
                ("petitioner_advocates_json", "TEXT"),
                ("respondent_advocates_json", "TEXT"),
                ("case_category_facet_path", "TEXT"),
                ("is_civil", "BOOLEAN DEFAULT 0"),
                ("final_rank", "INTEGER"),
            ]
            for col_name, col_type in ecourts_case_column_migrations:
                try:
                    await conn.execute(
                        text(f"ALTER TABLE ecourts_api_cases ADD COLUMN {col_name} {col_type}")
                    )
                    logger.info("Applied DB migration: ecourts_api_cases.%s", col_name)
                except Exception as exc:
                    msg = str(exc).lower()
                    if "duplicate column" in msg or "already exists" in msg:
                        logger.info("DB migration already applied: ecourts_api_cases.%s", col_name)
                    else:
                        logger.warning("DB migration skipped for ecourts_api_cases.%s: %s", col_name, exc)
            ecourts_call_column_migrations = [
                ("litigants_query", "TEXT"),
                ("search_filters_json", "TEXT"),
            ]
            for col_name, col_type in ecourts_call_column_migrations:
                try:
                    await conn.execute(
                        text(f"ALTER TABLE ecourts_api_calls ADD COLUMN {col_name} {col_type}")
                    )
                    logger.info("Applied DB migration: ecourts_api_calls.%s", col_name)
                except Exception as exc:
                    msg = str(exc).lower()
                    if "duplicate column" in msg or "already exists" in msg:
                        logger.info("DB migration already applied: ecourts_api_calls.%s", col_name)
                    else:
                        logger.warning("DB migration skipped for ecourts_api_calls.%s: %s", col_name, exc)
        await ensure_workflow_igr_hit_columns()
        logger.info("Database tables ready.")
        await _recover_orphaned_jobs()

    async def _recover_orphaned_jobs():
        """Mark any jobs still stuck in active states as failed.

        These are jobs whose asyncio task was killed mid-scrape by a server
        restart. Without this, they stay 'running' forever and block new
        searches via the concurrent scrape guard.
        """
        from sqlalchemy import select
        from datetime import datetime, timezone

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SearchJob).where(SearchJob.status.in_(["running", "pending"]))
            )
            orphans = result.scalars().all()
            for job in orphans:
                job.status = "failed"
                job.error_message = "Server restarted before scrape could complete."
                job.finished_at = datetime.now(timezone.utc)

            wf_result = await db.execute(
                select(LandCaseWorkflow).where(
                    LandCaseWorkflow.status.in_(
                        [
                            "pending_input",
                            "bhulekh_running",
                            "name_variants_ready",
                            "igr_running",
                            "ecourts_running",
                        ]
                    )
                )
            )
            wf_orphans = wf_result.scalars().all()
            for wf in wf_orphans:
                wf.status = "failed"
                wf.error_message = "Server restarted before workflow could complete."
                wf.finished_at = datetime.now(timezone.utc)

            if not orphans and not wf_orphans:
                return
            await db.commit()
            logger.warning(
                "Recovered %s orphaned eCourts job(s) and %s orphaned land workflow(s) → marked as failed.",
                len(orphans),
                len(wf_orphans),
            )

    # Health check
    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/health/db")
    async def health_db():
        required: dict[str, set[str]] = {
            "land_case_workflows": {
                "id",
                "district_label",
                "taluka_label",
                "village_label",
                "survey_part1",
                "survey_option_label",
                "owner_name_input",
                "status",
            },
            "ecourts_api_calls": {"id", "workflow_id", "owner_name_query", "endpoint", "method"},
            "ecourts_api_cases": {"id", "workflow_id", "cnr_number", "raw_json"},
            "ecourts_rank_cache": {"id", "owner_name_norm", "survey_token", "cached_ranked_json", "expires_at"},
            "workflow_case_hits": {"id", "workflow_id", "final_rank", "name_match_score"},
            "workflow_igr_hits": {"id", "workflow_id", "survey_number", "search_year"},
        }
        missing_tables: list[str] = []
        missing_columns: dict[str, list[str]] = {}
        try:
            async with engine.begin() as conn:
                for table, cols in required.items():
                    rows = await conn.execute(text(f'PRAGMA table_info("{table}")'))
                    existing = {r[1] for r in rows.fetchall()}
                    if not existing:
                        missing_tables.append(table)
                        continue
                    diff = sorted(c for c in cols if c not in existing)
                    if diff:
                        missing_columns[table] = diff
        except Exception as exc:
            logger.exception("DB health check failed.")
            return {
                "status": "error",
                "reason": "db_health_check_exception",
                "detail": str(exc),
            }

        if missing_tables or missing_columns:
            return {
                "status": "degraded",
                "reason": "schema_mismatch",
                "missing_tables": missing_tables,
                "missing_columns": missing_columns,
            }
        return {"status": "ok", "schema": "up_to_date"}

    # API routers
    app.include_router(jobs_router, prefix="/api")
    app.include_router(cases_router, prefix="/api")
    app.include_router(workflows_router, prefix="/api")

    # Serve frontend static files — must come last so API routes take priority
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    if os.path.isdir(static_dir):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app
