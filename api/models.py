"""SQLAlchemy ORM models."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, Text, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class SearchJob(Base):
    __tablename__ = "search_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
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
