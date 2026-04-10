"""FastAPI application factory."""

import os
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.database import engine, Base, AsyncSessionLocal
from api.models import SearchJob
from api.routes.jobs import router as jobs_router
from api.routes.cases import router as cases_router

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
        logger.info("Database tables ready.")
        await _recover_orphaned_jobs()

    async def _recover_orphaned_jobs():
        """Mark any jobs still stuck in 'running' as failed.

        These are jobs whose asyncio task was killed mid-scrape by a server
        restart. Without this, they stay 'running' forever and block new
        searches via the concurrent scrape guard.
        """
        from sqlalchemy import select
        from datetime import datetime, timezone

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SearchJob).where(SearchJob.status == "running")
            )
            orphans = result.scalars().all()
            if not orphans:
                return
            for job in orphans:
                job.status = "failed"
                job.error_message = "Server restarted while scrape was in progress."
                job.finished_at = datetime.now(timezone.utc)
            await db.commit()
            logger.warning(
                f"Recovered {len(orphans)} orphaned job(s) → marked as failed."
            )

    # Health check
    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    # API routers
    app.include_router(jobs_router, prefix="/api")
    app.include_router(cases_router, prefix="/api")

    # Serve frontend static files — must come last so API routes take priority
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    if os.path.isdir(static_dir):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app
