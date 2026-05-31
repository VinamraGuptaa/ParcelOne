"""SQLAlchemy ORM models."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, Text, ForeignKey, Index, Float, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    sessions: Mapped[list["AuthSession"]] = relationship(
        "AuthSession",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class AuthSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_token_hash", "token_hash", unique=True),
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped["User"] = relationship("User", back_populates="sessions")


class SearchJob(Base):
    __tablename__ = "search_jobs"
    __table_args__ = (Index("ix_search_jobs_user_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    petitioner_name: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/running/done/failed
    progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    years_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    years_done: Mapped[int] = mapped_column(Integer, default=0)
    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cases: Mapped[list["Case"]] = relationship("Case", back_populates="job", cascade="all, delete-orphan")


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (Index("ix_cases_job_id", "job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("search_jobs.id"), nullable=False)

    # Summary table fields
    search_year: Mapped[str | None] = mapped_column(Text, nullable=True)
    sr_no: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_type_number_year: Mapped[str | None] = mapped_column(Text, nullable=True)
    petitioner_vs_respondent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Detail page fields
    cnr_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    registration_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    registration_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    efiling_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    efiling_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    under_acts: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_hearing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_hearing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    nature_of_disposal: Mapped[str | None] = mapped_column(Text, nullable=True)
    court_number_judge: Mapped[str | None] = mapped_column(Text, nullable=True)
    petitioner_and_advocate: Mapped[str | None] = mapped_column(Text, nullable=True)
    respondent_and_advocate: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Full scraped dict as JSON — forward compatibility
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped["SearchJob"] = relationship("SearchJob", back_populates="cases")


class LandCaseWorkflow(Base):
    __tablename__ = "land_case_workflows"
    __table_args__ = (
        Index("ix_land_case_workflows_status", "status"),
        Index("ix_land_case_workflows_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    district_label: Mapped[str] = mapped_column(Text, nullable=False)
    taluka_label: Mapped[str] = mapped_column(Text, nullable=False)
    village_label: Mapped[str] = mapped_column(Text, nullable=False)
    survey_part1: Mapped[str] = mapped_column(Text, nullable=False)
    survey_option_label: Mapped[str] = mapped_column(Text, nullable=False)
    owner_name_input: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="pending_input")
    progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    occupant_primary_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    mutation_numbers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    variant_count: Mapped[int] = mapped_column(Integer, default=0)
    years_total: Mapped[int] = mapped_column(Integer, default=15)
    years_done: Mapped[int] = mapped_column(Integer, default=0)
    total_hits: Mapped[int] = mapped_column(Integer, default=0)
    ecourts_api_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    entities: Mapped[list["LandEntity"]] = relationship(
        "LandEntity",
        back_populates="workflow",
        cascade="all, delete-orphan",
    )
    variants: Mapped[list["NameVariant"]] = relationship(
        "NameVariant",
        back_populates="workflow",
        cascade="all, delete-orphan",
    )
    hits: Mapped[list["WorkflowCaseHit"]] = relationship(
        "WorkflowCaseHit",
        back_populates="workflow",
        cascade="all, delete-orphan",
    )


class LandEntity(Base):
    __tablename__ = "land_entities"
    __table_args__ = (Index("ix_land_entities_workflow_id", "workflow_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("land_case_workflows.id"),
        nullable=False,
    )

    occupant_primary_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    occupant_candidates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    mutation_numbers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="html")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    workflow: Mapped["LandCaseWorkflow"] = relationship("LandCaseWorkflow", back_populates="entities")


class NameVariant(Base):
    __tablename__ = "name_variants"
    __table_args__ = (Index("ix_name_variants_workflow_id", "workflow_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("land_case_workflows.id"),
        nullable=False,
    )

    base_name: Mapped[str] = mapped_column(Text, nullable=False)
    variant_text: Mapped[str] = mapped_column(Text, nullable=False)
    variant_kind: Mapped[str] = mapped_column(String(32), default="normalized")
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    workflow: Mapped["LandCaseWorkflow"] = relationship("LandCaseWorkflow", back_populates="variants")


class WorkflowCaseHit(Base):
    __tablename__ = "workflow_case_hits"
    __table_args__ = (
        Index("ix_workflow_case_hits_workflow_id", "workflow_id"),
        Index("ix_workflow_case_hits_rank", "workflow_id", "final_rank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("land_case_workflows.id"),
        nullable=False,
    )

    search_year: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    cnr_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    court: Mapped[str | None] = mapped_column(Text, nullable=True)
    parties_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    matched_variant: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    name_match_score: Mapped[float] = mapped_column(Float, default=0.0)
    is_civil: Mapped[bool] = mapped_column(Boolean, default=False)
    final_rank: Mapped[int] = mapped_column(Integer, default=0)

    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    workflow: Mapped["LandCaseWorkflow"] = relationship("LandCaseWorkflow", back_populates="hits")


class WorkflowIgrHit(Base):
    __tablename__ = "workflow_igr_hits"
    __table_args__ = (
        Index("ix_workflow_igr_hits_workflow_id", "workflow_id"),
        Index("ix_workflow_igr_hits_survey_year", "workflow_id", "survey_number", "search_year"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("land_case_workflows.id"),
        nullable=False,
    )
    survey_number: Mapped[str] = mapped_column(Text, nullable=False)
    search_year: Mapped[str] = mapped_column(Text, nullable=False)
    district_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    taluka_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    village_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_region: Mapped[str] = mapped_column(String(32), default="rest_of_maharashtra")
    # Structured transaction fields (doc_no, seller, buyer, etc.) live in raw_json
    # so existing deployments never need a schema migration to serve results.
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EcourtsApiCall(Base):
    __tablename__ = "ecourts_api_calls"
    __table_args__ = (
        Index("ix_ecourts_api_calls_workflow_id", "workflow_id"),
        Index("ix_ecourts_api_calls_owner_name_query", "owner_name_query"),
        Index("ix_ecourts_api_calls_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(String(36), ForeignKey("land_case_workflows.id"), nullable=False)
    owner_name_query: Mapped[str] = mapped_column(Text, nullable=False)
    request_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    litigants_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_filters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_success: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EcourtsApiCase(Base):
    __tablename__ = "ecourts_api_cases"
    __table_args__ = (
        Index("ix_ecourts_api_cases_workflow_id", "workflow_id"),
        Index("ix_ecourts_api_cases_cnr_number", "cnr_number"),
        Index("ix_ecourts_api_cases_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(String(36), ForeignKey("land_case_workflows.id"), nullable=False)
    cnr_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_type_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    court: Mapped[str | None] = mapped_column(Text, nullable=True)
    court_no: Mapped[str | None] = mapped_column(Text, nullable=True)
    district: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    cnr_year: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    registration_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    registration_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_hearing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_hearing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    petitioners_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    respondents_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    petitioner_advocates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    respondent_advocates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_category_facet_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    parties_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_civil: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    final_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_stage: Mapped[str] = mapped_column(String(24), default="search")
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EcourtsRankCache(Base):
    __tablename__ = "ecourts_rank_cache"
    __table_args__ = (
        Index(
            "ix_ecourts_rank_cache_lookup",
            "owner_name_norm",
            "district_label",
            "taluka_label",
            "village_label",
            "survey_token",
            "source_mode",
        ),
        Index("ix_ecourts_rank_cache_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    district_label: Mapped[str] = mapped_column(Text, nullable=False)
    taluka_label: Mapped[str] = mapped_column(Text, nullable=False)
    village_label: Mapped[str] = mapped_column(Text, nullable=False)
    survey_token: Mapped[str] = mapped_column(Text, nullable=False)
    source_mode: Mapped[str] = mapped_column(String(16), default="api")
    cached_ranked_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
