from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from api.database import AsyncSessionLocal, Base, engine
from api.land_case_worker import run_land_case_workflow
from api.models import (
    EcourtsApiCall,
    EcourtsApiCase,
    LandCaseWorkflow,
    WorkflowCaseHit,
    WorkflowIgrHit,
)

OWNER_HINT = "Snehal Bhooshan Dhoot"
DISTRICT = "Pune"
TALUKA = "Haveli"
VILLAGE = "Wagholi"
SURVEY_PART1 = "1530"
SURVEY_OPTION = "1530/3"


def _ts_of(lines: list[str], needle: str) -> datetime | None:
    for ln in lines:
        if needle in ln:
            try:
                return datetime.strptime(ln.split(" - ")[0], "%Y-%m-%d %H:%M:%S,%f").replace(
                    tzinfo=timezone.utc
                )
            except Exception:
                return None
    return None


def _dur(a: datetime | None, b: datetime | None) -> float | None:
    if not a or not b:
        return None
    return round((b - a).total_seconds(), 2)


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    workflow_id = None
    t0 = datetime.now(timezone.utc)
    t1 = None

    try:
        async with AsyncSessionLocal() as db:
            wf = LandCaseWorkflow(
                district_label=DISTRICT,
                taluka_label=TALUKA,
                village_label=VILLAGE,
                survey_part1=SURVEY_PART1,
                survey_option_label=SURVEY_OPTION,
                status="pending_input",
                progress_message="Manual rerun requested",
            )
            db.add(wf)
            await db.commit()
            await db.refresh(wf)
            workflow_id = wf.id

        await asyncio.wait_for(run_land_case_workflow(workflow_id), timeout=3600)
        t1 = datetime.now(timezone.utc)
    except Exception as exc:
        t1 = datetime.now(timezone.utc)
        logging.getLogger(__name__).exception("Flow run terminated with exception: %s", exc)
    finally:
        try:
            async with AsyncSessionLocal() as db:
                wf = (
                    await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
                ).scalar_one_or_none()
                igr_hits = (
                    await db.execute(select(WorkflowIgrHit).where(WorkflowIgrHit.workflow_id == workflow_id))
                ).scalars().all()
                case_hits = (
                    await db.execute(select(WorkflowCaseHit).where(WorkflowCaseHit.workflow_id == workflow_id))
                ).scalars().all()
                api_calls = (
                    await db.execute(select(EcourtsApiCall).where(EcourtsApiCall.workflow_id == workflow_id))
                ).scalars().all()
                api_cases = (
                    await db.execute(select(EcourtsApiCase).where(EcourtsApiCase.workflow_id == workflow_id))
                ).scalars().all()
        except Exception:
            wf = None
            igr_hits = []
            case_hits = []
            api_calls = []
            api_cases = []

        lines = log_stream.getvalue().splitlines()
        bhulekh_start = _ts_of(lines, "Stage bhulekh_running started")
        bhulekh_end = _ts_of(lines, "Name variants generated")
        igr_start = _ts_of(lines, "Stage igr_running started")
        igr_end = _ts_of(lines, "IGR stage completed")
        ec_start = _ts_of(lines, "Stage ecourts_running started")
        ec_end = _ts_of(lines, "Workflow completed successfully") or _ts_of(lines, "Ranking complete")

        summary = {
            "workflow_id": workflow_id,
            "inputs": {
                "owner_name_hint": OWNER_HINT,
                "district_label": DISTRICT,
                "taluka_label": TALUKA,
                "village_label": VILLAGE,
                "survey_part1": SURVEY_PART1,
                "survey_option_label": SURVEY_OPTION,
            },
            "status": (wf.status if wf else None),
            "error_message": (wf.error_message if wf else "workflow row unavailable"),
            "overall_seconds": round(((t1 or datetime.now(timezone.utc)) - t0).total_seconds(), 2),
            "step_timings_seconds": {
                "bhulekh_and_extraction": _dur(bhulekh_start, bhulekh_end),
                "igr": _dur(igr_start, igr_end),
                "ecourts_and_ranking": _dur(ec_start, ec_end),
            },
            "result_counts": {
                "igr_hits": len(igr_hits),
                "ranked_case_hits": len(case_hits),
                "ecourts_api_calls": len(api_calls),
                "ecourts_api_cases": len(api_cases),
            },
            "artifacts": {
                "pdf_path": (wf.pdf_path if wf else None),
                "html_path": (wf.html_path if wf else None),
                "survey_options_json": (
                    str(Path("artifacts/workflows") / f"{workflow_id}_survey_options.json")
                    if workflow_id
                    else None
                ),
                "summary_json": (
                    str(Path("artifacts/workflows") / f"{workflow_id}_run2_summary.json")
                    if workflow_id
                    else None
                ),
            },
            "logs_path": (
                str(Path("artifacts/workflows") / f"{workflow_id}_run2.log") if workflow_id else None
            ),
        }

        out_dir = Path("artifacts/workflows")
        out_dir.mkdir(parents=True, exist_ok=True)
        if workflow_id:
            (out_dir / f"{workflow_id}_run2.log").write_text(log_stream.getvalue(), encoding="utf-8")
            (out_dir / f"{workflow_id}_run2_summary.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        print(json.dumps(summary, ensure_ascii=False))

        root.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    asyncio.run(main())
