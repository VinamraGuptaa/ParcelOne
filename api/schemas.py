"""Pydantic v2 request/response schemas."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, field_validator, computed_field


# ---------- Request ----------

class JobCreateRequest(BaseModel):
    petitioner_name: str
    year: Optional[str] = None

    @field_validator("petitioner_name")
    @classmethod
    def name_min_length(cls, v: str) -> str:
        if len(v.strip()) < 3:
            raise ValueError("Petitioner name must be at least 3 characters.")
        return v.strip()

    @field_validator("year")
    @classmethod
    def year_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if not v:
                return None
            if not v.isdigit() or len(v) != 4:
                raise ValueError("Year must be a 4-digit number.")
        return v


# ---------- Response: Job ----------

class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    petitioner_name: str
    year: Optional[str]
    status: str
    progress_message: Optional[str]
    years_total: Optional[int]
    years_done: int
    total_cases: int
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]

    @computed_field
    @property
    def progress_pct(self) -> int:
        if self.years_total and self.years_total > 0:
            return int(self.years_done / self.years_total * 100)
        return 0

    @classmethod
    def from_orm_obj(cls, obj) -> "JobResponse":
        return cls(
            job_id=obj.id,
            petitioner_name=obj.petitioner_name,
            year=obj.year,
            status=obj.status,
            progress_message=obj.progress_message,
            years_total=obj.years_total,
            years_done=obj.years_done,
            total_cases=obj.total_cases,
            error_message=obj.error_message,
            created_at=obj.created_at,
            started_at=obj.started_at,
            finished_at=obj.finished_at,
        )


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int


# ---------- Response: Case ----------

class CaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    search_year: Optional[str]
    sr_no: Optional[str]
    case_type_number_year: Optional[str]
    petitioner_vs_respondent: Optional[str]
    cnr_number: Optional[str]
    case_type: Optional[str]
    filing_number: Optional[str]
    filing_date: Optional[str]
    registration_number: Optional[str]
    registration_date: Optional[str]
    efiling_number: Optional[str]
    efiling_date: Optional[str]
    under_acts: Optional[str]
    first_hearing_date: Optional[str]
    next_hearing_date: Optional[str]
    case_stage: Optional[str]
    decision_date: Optional[str]
    case_status: Optional[str]
    nature_of_disposal: Optional[str]
    court_number_judge: Optional[str]
    petitioner_and_advocate: Optional[str]
    respondent_and_advocate: Optional[str]
    created_at: datetime


class CasesListResponse(BaseModel):
    job_id: str
    cases: list[CaseResponse]
    total: int
