"""Routes for unified land-to-cases workflow orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.land_case_worker import (
    _is_plausible_ecourts_name,
    _split_party_name_blob,
    parse_igr_hit_raw,
    run_land_case_workflow,
)
from api.models import (
    EcourtsApiCall,
    EcourtsApiCase,
    LandCaseWorkflow,
    LandEntity,
    NameVariant,
    WorkflowCaseHit,
    WorkflowIgrHit,
)
from api.schemas import (
    EcourtsApiCallResponse,
    EcourtsApiCaseResponse,
    IgrTransactionEntry,
    LandCaseWorkflowArtifactsResponse,
    LandCaseWorkflowCreateRequest,
    LandCaseWorkflowResponse,
    LandCaseWorkflowResultsResponse,
    LandEntityResponse,
    LitigationSignalEntry,
    NameVariantResponse,
    WorkflowCaseHitResponse,
    WorkflowIgrHitResponse,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])


# ── Due-diligence helpers ──────────────────────────────────────────────────

def _fmt_igr_date(raw: str) -> str:
    """Parse DD/MM/YYYY → 'Month YYYY'; return raw string on failure."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw, "%d/%m/%Y")
        return dt.strftime("%B %Y")
    except Exception:
        return raw


def _igr_date_year(raw: str) -> int | None:
    """Extract the year integer from a DD/MM/YYYY string."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d/%m/%Y").year
    except Exception:
        return None


def _names_in_parties(names: list[str], parties_text: str) -> bool:
    """Return True if any of *names* appears (word-level) in parties_text."""
    if not parties_text:
        return False
    pt = re.sub(r"[^\w\s]", " ", parties_text.lower())
    for name in names:
        tokens = [t for t in re.sub(r"[^\w\s]", " ", name.lower()).split() if len(t) >= 3]
        if tokens and all(t in pt for t in tokens):
            return True
    return False


def _case_year(c: EcourtsApiCase) -> str | None:
    """Best-effort year string for a case (from filing, registration, or cnr)."""
    for raw in (c.filing_date, c.registration_date):
        if raw:
            try:
                return str(datetime.strptime(raw.strip(), "%d/%m/%Y").year)
            except Exception:
                pass
    if c.cnr_year:
        return str(c.cnr_year)
    return None


def _case_relevance(rank: int | None, is_pending: bool) -> str:
    if rank is not None and rank <= 2:
        return "high"
    if (rank is not None and rank <= 5) or is_pending:
        return "medium"
    return "low"


def _build_due_diligence(
    igr_rows: list[WorkflowIgrHit],
    raw_api_cases: list[EcourtsApiCase],
    occupant_primary_name: str | None,
) -> dict:
    """
    Build ownership_timeline, litigation_signals, current_owner,
    total_transactions, title_period_years, and flagged from raw DB rows.
    """
    # ── Litigation signals from ranked eCourts cases ──────────────────────
    signals: list[LitigationSignalEntry] = []
    for c in raw_api_cases:
        if c.final_rank is None:
            continue
        pets = json.loads(c.petitioners_json or "[]")
        resps = json.loads(c.respondents_json or "[]")
        if pets or resps:
            parties_display = (
                f"{pets[0]} v. {resps[0]}"
                if pets and resps
                else (pets[0] if pets else resps[0] if resps else (c.parties_text or ""))
            )
        else:
            parties_display = c.parties_text or ""
        signals.append(LitigationSignalEntry(
            parties=parties_display,
            case_type=c.case_type or c.case_type_raw,
            court=c.court,
            year=_case_year(c),
            cnr_number=c.cnr_number,
            case_status=c.case_status,
            is_pending=bool(c.is_pending),
            relevance=_case_relevance(c.final_rank, bool(c.is_pending)),
            final_rank=c.final_rank,
        ))
    signals.sort(key=lambda s: (s.final_rank is None, s.final_rank or 10**9))

    # Collect all parties text for litigation-linked checking
    all_parties_texts = [c.parties_text or "" for c in raw_api_cases if c.parties_text]

    # ── Ownership timeline from IGR rows ──────────────────────────────────
    transactions: list[IgrTransactionEntry] = []
    earliest_year: int | None = None
    last_buyer: str | None = None

    for h in igr_rows:
        raw = json.loads(h.raw_json or "{}")
        fields = parse_igr_hit_raw(raw)
        doc_no = fields["doc_no"]
        doc_type_m = fields["doc_type_marathi"]
        doc_type_en = fields["doc_type"]
        reg_date_raw = fields["reg_date"]
        sro_name = fields["sro_name"]
        sellers = fields["sellers"]
        buyers = fields["buyers"]
        seller_display = fields["seller"]
        buyer_display = fields["buyer"]

        yr = _igr_date_year(reg_date_raw)
        if yr:
            if earliest_year is None or yr < earliest_year:
                earliest_year = yr
        if buyer_display:
            last_buyer = buyer_display

        linked = _names_in_parties(
            [n for n in sellers + buyers if n],
            " ".join(all_parties_texts),
        )

        transactions.append(IgrTransactionEntry(
            doc_no=doc_no,
            doc_type=doc_type_en,
            doc_type_marathi=doc_type_m,
            reg_date=reg_date_raw,
            reg_date_fmt=_fmt_igr_date(reg_date_raw),
            sro_name=sro_name,
            seller=seller_display,
            buyer=buyer_display,
            year=h.search_year,
            litigation_linked=linked,
        ))

    # Sort newest first
    def _txn_sort_key(t: IgrTransactionEntry):
        try:
            return (-datetime.strptime(t.reg_date, "%d/%m/%Y").timestamp(),)
        except Exception:
            return (0,)

    transactions.sort(key=_txn_sort_key)

    current_owner = occupant_primary_name or last_buyer
    now_year = datetime.now(timezone.utc).year
    title_period = (now_year - earliest_year) if earliest_year else None

    return {
        "ownership_timeline": transactions,
        "litigation_signals": signals,
        "current_owner": current_owner,
        "total_transactions": len(transactions),
        "title_period_years": title_period,
        "flagged": len(signals) > 0,
    }
logger = logging.getLogger(__name__)
ACTIVE_WORKFLOW_STATUSES = (
    "pending_input",
    "bhulekh_running",
    "name_variants_ready",
    "igr_running",
    "ecourts_running",
)


def _load_survey_options_for_workflow(wf: LandCaseWorkflow) -> list[str]:
    if not wf.html_path:
        return []
    try:
        from pathlib import Path

        from api.land_case_flow import extract_survey_option_labels

        html = Path(wf.html_path).read_text(encoding="utf-8")
        return extract_survey_option_labels(html, wf.survey_part1)
    except Exception:
        return []


def _workflow_to_response(wf: LandCaseWorkflow) -> LandCaseWorkflowResponse:
    metrics = None
    try:
        metrics = json.loads(wf.ecourts_api_metrics_json or "null")
    except Exception:
        metrics = None
    return LandCaseWorkflowResponse(
        workflow_id=wf.id,
        status=wf.status,
        progress_message=wf.progress_message,
        error_message=wf.error_message,
        district_label=wf.district_label,
        taluka_label=wf.taluka_label,
        village_label=wf.village_label,
        survey_part1=wf.survey_part1,
        survey_option_label=wf.survey_option_label,
        owner_name=wf.owner_name_input,
        occupant_primary_name=wf.occupant_primary_name,
        extraction_confidence=wf.extraction_confidence,
        years_total=wf.years_total,
        years_done=wf.years_done,
        total_hits=wf.total_hits,
        ecourts_api_metrics=metrics,
        created_at=wf.created_at,
        started_at=wf.started_at,
        finished_at=wf.finished_at,
    )


ARTIFACTS_ROOT = Path("artifacts/workflows").resolve()

# Mapping of {kind -> (default filename suffix, media type, download filename)}
ARTIFACT_KINDS: dict[str, tuple[str, str, str]] = {
    "pdf": ("_land_record.pdf", "application/pdf", "land_record.pdf"),
    "csv": ("_ranked_hits.csv", "text/csv", "ranked_hits.csv"),
    "unranked_csv": ("_unranked_raw.csv", "text/csv", "unranked_raw.csv"),
    "html": ("_submitted.html", "text/html", "submitted.html"),
}


def _ranked_csv_path_for_workflow(workflow_id: str) -> str | None:
    """Return the ranked-CSV path as a workspace-relative string (or None).

    Kept relative for backward compatibility with the API contract: existing
    consumers (and the API test suite) expect ``artifacts/workflows/{id}_*``.
    Security checks for serving the file go through
    :func:`_resolve_artifact_path` which uses the absolute ``ARTIFACTS_ROOT``.
    """
    rel = Path("artifacts/workflows") / f"{workflow_id}_ranked_hits.csv"
    abs_path = ARTIFACTS_ROOT / f"{workflow_id}_ranked_hits.csv"
    return str(rel) if abs_path.exists() else None


def _resolve_artifact_path(workflow_id: str, kind: str, wf: LandCaseWorkflow) -> Path | None:
    """Resolve an artifact path for a workflow.

    Validates the path lives under ``ARTIFACTS_ROOT`` (defence in depth against
    a hostile path stored in DB). Falls back to common artifact filenames when
    the DB column for the kind is missing. Returns None if no file matches.
    """
    candidates: list[Path] = []
    if kind == "pdf" and wf.pdf_path:
        candidates.append(Path(wf.pdf_path))
    elif kind == "html" and wf.html_path:
        candidates.append(Path(wf.html_path))

    suffix, _, _ = ARTIFACT_KINDS[kind]
    candidates.append(ARTIFACTS_ROOT / f"{workflow_id}{suffix}")

    if kind == "pdf":
        candidates.append(ARTIFACTS_ROOT / f"{workflow_id}.pdf")
        candidates.append(ARTIFACTS_ROOT / f"{workflow_id}_bhulekh.pdf")
    elif kind == "html":
        candidates.append(ARTIFACTS_ROOT / f"{workflow_id}.html")

    for cand in candidates:
        try:
            resolved = cand.resolve()
        except (OSError, RuntimeError):
            continue
        try:
            resolved.relative_to(ARTIFACTS_ROOT)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


@router.post("/land-case-search", status_code=202, response_model=LandCaseWorkflowResponse)
async def create_land_case_workflow(
    body: LandCaseWorkflowCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    logger.info(
        "Create land workflow request: district=%r taluka=%r village=%r survey=%r/%r owner=%r idem=%r",
        body.district_label,
        body.taluka_label,
        body.village_label,
        body.survey_part1,
        body.survey_option_label,
        body.owner_name,
        body.idempotency_key,
    )
    if body.idempotency_key:
        existing = await db.execute(
            select(LandCaseWorkflow).where(LandCaseWorkflow.idempotency_key == body.idempotency_key)
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            logger.info("Idempotent land workflow hit: workflow_id=%s", found.id)
            return _workflow_to_response(found)

    active = await db.execute(
        select(LandCaseWorkflow.id).where(LandCaseWorkflow.status.in_(ACTIVE_WORKFLOW_STATUSES)).limit(1)
    )
    active_workflow_id = active.scalar_one_or_none()
    if active_workflow_id is not None:
        logger.info(
            "Rejecting land workflow create while active workflow exists: active_workflow_id=%s",
            active_workflow_id,
        )
        raise HTTPException(
            status_code=409,
            detail="Another land workflow is already in progress. Please try again shortly.",
        )

    wf = LandCaseWorkflow(
        district_label=body.district_label,
        taluka_label=body.taluka_label,
        village_label=body.village_label,
        survey_part1=body.survey_part1,
        survey_option_label=body.survey_option_label,
        owner_name_input=body.owner_name,
        idempotency_key=body.idempotency_key,
        status="pending_input",
        progress_message="Workflow accepted.",
    )
    db.add(wf)
    await db.commit()
    await db.refresh(wf)

    asyncio.create_task(run_land_case_workflow(wf.id))
    logger.info("Created land workflow: workflow_id=%s status=%s", wf.id, wf.status)
    return _workflow_to_response(wf)


@router.get("/{workflow_id}", response_model=LandCaseWorkflowResponse)
async def get_land_case_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
    wf = result.scalar_one_or_none()
    if wf is None:
        logger.warning("Workflow status requested but not found: workflow_id=%s", workflow_id)
        raise HTTPException(status_code=404, detail="Workflow not found.")
    logger.info("Workflow status fetched: workflow_id=%s status=%s", workflow_id, wf.status)
    return _workflow_to_response(wf)


@router.get("/{workflow_id}/results", response_model=LandCaseWorkflowResultsResponse)
async def get_land_case_workflow_results(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
):
    wf_result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
    wf = wf_result.scalar_one_or_none()
    if wf is None:
        logger.warning("Workflow results requested but not found: workflow_id=%s", workflow_id)
        raise HTTPException(status_code=404, detail="Workflow not found.")

    ent_result = await db.execute(
        select(LandEntity)
        .where(LandEntity.workflow_id == workflow_id)
        .order_by(LandEntity.id.desc())
        .limit(1)
    )
    ent = ent_result.scalar_one_or_none()
    entity_response = None
    if ent is not None:
        raw_candidates: list[str] = json.loads(ent.occupant_candidates_json or "[]")
        filtered_candidates = [c for c in raw_candidates if _is_plausible_ecourts_name(c)]
        entity_response = LandEntityResponse(
            occupant_primary_name=ent.occupant_primary_name,
            occupant_candidates=filtered_candidates,
            mutation_numbers=json.loads(ent.mutation_numbers_json or "[]"),
            extraction_confidence=ent.extraction_confidence or 0.0,
        )

    var_result = await db.execute(
        select(NameVariant)
        .where(NameVariant.workflow_id == workflow_id)
        .order_by(NameVariant.quality_score.desc(), NameVariant.id.asc())
    )
    variants = [
        NameVariantResponse(
            variant_text=v.variant_text,
            variant_kind=v.variant_kind,
            quality_score=v.quality_score,
        )
        for v in var_result.scalars().all()
    ]

    hit_result = await db.execute(
        select(WorkflowCaseHit)
        .where(WorkflowCaseHit.workflow_id == workflow_id)
        .order_by(WorkflowCaseHit.final_rank.asc(), WorkflowCaseHit.id.asc())
    )
    hits = [
        WorkflowCaseHitResponse(
            search_year=h.search_year,
            case_id=h.case_id,
            cnr_number=h.cnr_number,
            case_type=h.case_type,
            court=h.court,
            parties_text=h.parties_text,
            is_civil=h.is_civil,
            name_match_score=h.name_match_score,
            matched_variant=h.matched_variant,
            match_explanation=h.match_explanation,
            final_rank=h.final_rank,
        )
        for h in hit_result.scalars().all()
    ]
    igr_result = await db.execute(
        select(WorkflowIgrHit)
        .where(WorkflowIgrHit.workflow_id == workflow_id)
        .order_by(
            WorkflowIgrHit.survey_number.asc(),
            WorkflowIgrHit.search_year.desc(),
            WorkflowIgrHit.id.asc(),
        )
    )
    igr_rows = list(igr_result.scalars().all())
    igr_purchaser_names: list[str] = []
    _igr_purch_seen: set[str] = set()
    for h in igr_rows:
        raw_dict = json.loads(h.raw_json or "{}")
        for key in ("purchaser_name", "Purchaser Name", "PurchaserName"):
            value = raw_dict.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            for part in _split_party_name_blob(value):
                p = part.strip()
                if p and p not in _igr_purch_seen:
                    _igr_purch_seen.add(p)
                    igr_purchaser_names.append(p)
            break
    igr_hits = [
        WorkflowIgrHitResponse(
            survey_number=h.survey_number,
            search_year=h.search_year,
            district_label=h.district_label,
            taluka_label=h.taluka_label,
            village_label=h.village_label,
            source_region=h.source_region,
            raw=json.loads(h.raw_json or "{}"),
        )
        for h in igr_rows
    ]
    api_calls_result = await db.execute(
        select(EcourtsApiCall)
        .where(EcourtsApiCall.workflow_id == workflow_id)
        .order_by(EcourtsApiCall.id.asc())
    )
    api_calls = [
        EcourtsApiCallResponse(
            request_kind=c.request_kind,
            endpoint=c.endpoint,
            method=c.method,
            litigants_query=c.litigants_query,
            search_filters=json.loads(c.search_filters_json or "null"),
            response_status=c.response_status,
            provider_error_code=c.provider_error_code,
            retryable=c.retryable,
            is_success=c.is_success,
        )
        for c in api_calls_result.scalars().all()
    ]
    api_cases_result = await db.execute(
        select(EcourtsApiCase)
        .where(EcourtsApiCase.workflow_id == workflow_id)
        .order_by(EcourtsApiCase.id.asc())
    )
    raw_api_cases = list(api_cases_result.scalars().all())
    raw_api_cases_sorted = sorted(
        raw_api_cases,
        key=lambda row: (row.final_rank is None, row.final_rank or 10**9, row.id),
    )
    api_cases = [
        EcourtsApiCaseResponse(
            cnr_number=c.cnr_number,
            case_type=c.case_type,
            case_type_raw=c.case_type_raw,
            court=c.court,
            court_no=c.court_no,
            district=c.district,
            state=c.state,
            case_number=c.case_number,
            cnr_year=c.cnr_year,
            filing_number=c.filing_number,
            filing_date=c.filing_date,
            registration_number=c.registration_number,
            registration_date=c.registration_date,
            first_hearing_date=c.first_hearing_date,
            next_hearing_date=c.next_hearing_date,
            decision_date=c.decision_date,
            petitioners=json.loads(c.petitioners_json or "[]"),
            respondents=json.loads(c.respondents_json or "[]"),
            petitioner_advocates=json.loads(c.petitioner_advocates_json or "[]"),
            respondent_advocates=json.loads(c.respondent_advocates_json or "[]"),
            case_category_facet_path=c.case_category_facet_path,
            parties_text=c.parties_text,
            case_status=c.case_status,
            is_civil=c.is_civil,
            is_pending=c.is_pending,
            final_rank=c.final_rank,
            source_stage=c.source_stage,
            raw=json.loads(c.raw_json or "{}"),
        )
        for c in raw_api_cases_sorted
    ]

    # ── Due-diligence report data ─────────────────────────────────────────
    dd = _build_due_diligence(
        igr_rows=igr_rows,
        raw_api_cases=raw_api_cases_sorted,
        occupant_primary_name=entity_response.occupant_primary_name if entity_response else None,
    )

    return LandCaseWorkflowResultsResponse(
        workflow_id=workflow_id,
        district_label=wf.district_label,
        taluka_label=wf.taluka_label,
        village_label=wf.village_label,
        survey_option_label=wf.survey_option_label,
        owner_name=wf.owner_name_input,
        entity=entity_response,
        variants=variants,
        survey_options=_load_survey_options_for_workflow(wf),
        hits=hits,
        igr_hits=igr_hits,
        igr_purchaser_names=igr_purchaser_names,
        total_hits=len(hits),
        ecourts_api_metrics=json.loads(wf.ecourts_api_metrics_json or "null"),
        ecourts_api_calls=api_calls,
        ecourts_api_cases=api_cases,
        **dd,
    )


@router.get("/{workflow_id}/artifacts", response_model=LandCaseWorkflowArtifactsResponse)
async def get_land_case_workflow_artifacts(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
    wf = result.scalar_one_or_none()
    if wf is None:
        logger.warning("Workflow artifacts requested but not found: workflow_id=%s", workflow_id)
        raise HTTPException(status_code=404, detail="Workflow not found.")
    logger.info(
        "Workflow artifacts fetched: workflow_id=%s has_pdf=%s has_html=%s",
        workflow_id,
        bool(wf.pdf_path),
        bool(wf.html_path),
    )
    return LandCaseWorkflowArtifactsResponse(
        workflow_id=workflow_id,
        pdf_path=wf.pdf_path,
        html_path=wf.html_path,
        ranked_csv_path=_ranked_csv_path_for_workflow(workflow_id),
    )


@router.get("/{workflow_id}/artifact/{kind}")
async def stream_land_case_workflow_artifact(
    workflow_id: str,
    kind: str,
    db: AsyncSession = Depends(get_db),
):
    """Stream a workflow artifact (pdf | csv | html) by id.

    Avoids exposing the on-disk ``artifacts/workflows/`` directory directly:
    we resolve the file via DB metadata, sanity-check it lives under
    ``ARTIFACTS_ROOT``, and stream it with the correct ``Content-Type``.
    """
    kind_norm = (kind or "").lower().strip()
    if kind_norm not in ARTIFACT_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported artifact kind {kind!r}. Allowed: pdf, csv, html.",
        )

    result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
    wf = result.scalar_one_or_none()
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")

    path = _resolve_artifact_path(workflow_id, kind_norm, wf)
    if path is None:
        logger.info(
            "Artifact not found: workflow_id=%s kind=%s", workflow_id, kind_norm
        )
        raise HTTPException(
            status_code=404, detail=f"No {kind_norm} artifact for this workflow."
        )

    _, media_type, default_filename = ARTIFACT_KINDS[kind_norm]
    download_name = f"{workflow_id}_{default_filename}"
    logger.info(
        "Streaming artifact: workflow_id=%s kind=%s path=%s",
        workflow_id,
        kind_norm,
        path,
    )
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=download_name,
    )
