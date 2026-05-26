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


class LandCaseWorkflowCreateRequest(BaseModel):
    district_label: Optional[str] = "Pune"
    taluka_label: Optional[str] = "Haveli"
    village_label: str
    survey_part1: str
    survey_option_label: str
    owner_name: Optional[str] = None
    idempotency_key: Optional[str] = None

    @field_validator(
        "district_label",
        "taluka_label",
        "village_label",
        "survey_part1",
        "survey_option_label",
    )
    @classmethod
    def required_fields_not_blank(cls, v: str) -> str:
        val = (v or "").strip()
        if not val:
            raise ValueError("Field must not be blank.")
        return val

    @field_validator("district_label", "taluka_label")
    @classmethod
    def default_location_when_missing(cls, v: Optional[str], info) -> str:
        val = (v or "").strip()
        if val:
            return val
        if info.field_name == "district_label":
            return "Pune"
        return "Haveli"

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        out = v.strip()
        if not out:
            return None
        if len(out) > 128:
            raise ValueError("idempotency_key must be <= 128 chars.")
        return out

    @field_validator("owner_name")
    @classmethod
    def normalize_owner_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        out = v.strip()
        return out or None


class LandEntityResponse(BaseModel):
    occupant_primary_name: Optional[str]
    occupant_candidates: list[str]
    mutation_numbers: list[str]
    extraction_confidence: float


class NameVariantResponse(BaseModel):
    variant_text: str
    variant_kind: str
    quality_score: float


class WorkflowCaseHitResponse(BaseModel):
    search_year: Optional[str]
    case_id: Optional[str]
    cnr_number: Optional[str]
    case_type: Optional[str]
    court: Optional[str]
    parties_text: Optional[str]
    is_civil: bool
    name_match_score: float
    matched_variant: Optional[str]
    match_explanation: Optional[str]
    final_rank: int


class WorkflowIgrHitResponse(BaseModel):
    survey_number: str
    search_year: str
    district_label: Optional[str]
    taluka_label: Optional[str]
    village_label: Optional[str]
    source_region: str
    raw: dict


class EcourtsApiCallResponse(BaseModel):
    request_kind: str
    endpoint: str
    method: str
    litigants_query: Optional[str]
    search_filters: Optional[dict] = None
    response_status: Optional[int]
    provider_error_code: Optional[str]
    retryable: Optional[bool]
    is_success: bool


class EcourtsApiCaseResponse(BaseModel):
    cnr_number: Optional[str]
    case_type: Optional[str]
    case_type_raw: Optional[str]
    court: Optional[str]
    court_no: Optional[str]
    district: Optional[str]
    state: Optional[str]
    case_number: Optional[str]
    cnr_year: Optional[str]
    filing_number: Optional[str]
    filing_date: Optional[str]
    registration_number: Optional[str]
    registration_date: Optional[str]
    first_hearing_date: Optional[str]
    next_hearing_date: Optional[str]
    decision_date: Optional[str]
    petitioners: list[str] = []
    respondents: list[str] = []
    petitioner_advocates: list[str] = []
    respondent_advocates: list[str] = []
    case_category_facet_path: Optional[str]
    parties_text: Optional[str]
    case_status: Optional[str]
    is_civil: bool
    is_pending: bool
    final_rank: Optional[int]
    source_stage: str
    raw: dict


class LandCaseWorkflowResponse(BaseModel):
    workflow_id: str
    status: str
    progress_message: Optional[str]
    error_message: Optional[str]
    district_label: str
    taluka_label: str
    village_label: str
    survey_part1: str
    survey_option_label: str
    owner_name: Optional[str]
    occupant_primary_name: Optional[str]
    extraction_confidence: Optional[float]
    years_total: int
    years_done: int
    total_hits: int
    ecourts_api_metrics: Optional[dict] = None
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]

    @computed_field
    @property
    def progress_pct(self) -> int:
        if self.years_total > 0:
            return int(self.years_done / self.years_total * 100)
        return 0


class IgrTransactionEntry(BaseModel):
    """One IGR registration record formatted for the ownership timeline."""
    doc_no: str
    doc_type: str           # English label
    doc_type_marathi: str   # original DName
    reg_date: str           # raw DD/MM/YYYY
    reg_date_fmt: str       # human-readable "March 2019"
    sro_name: str
    seller: str             # first seller name (cleaned)
    buyer: str              # first purchaser name (cleaned)
    year: str               # search_year
    litigation_linked: bool = False


class LitigationSignalEntry(BaseModel):
    """One eCourts case displayed as a litigation signal."""
    parties: str
    case_type: Optional[str]
    court: Optional[str]
    year: Optional[str]
    cnr_number: Optional[str]
    case_status: Optional[str]
    is_pending: bool
    relevance: str          # "high" | "medium" | "low"
    final_rank: Optional[int]


class LandCaseWorkflowResultsResponse(BaseModel):
    workflow_id: str
    # Location fields mirrored from the workflow record for the UI report header.
    district_label: Optional[str] = None
    taluka_label: Optional[str] = None
    village_label: Optional[str] = None
    survey_option_label: Optional[str] = None
    owner_name: Optional[str]
    entity: Optional[LandEntityResponse]
    variants: list[NameVariantResponse]
    survey_options: list[str]
    hits: list[WorkflowCaseHitResponse]
    igr_hits: list[WorkflowIgrHitResponse]
    # Purchaser / party names from IGR hit rows (deduplicated; split multi-name blobs).
    igr_purchaser_names: list[str] = []
    total_hits: int
    ecourts_api_metrics: Optional[dict] = None
    ecourts_api_calls: list[EcourtsApiCallResponse] = []
    ecourts_api_cases: list[EcourtsApiCaseResponse] = []

    # ── Due-diligence report fields ───────────────────────────────────────
    ownership_timeline: list[IgrTransactionEntry] = []
    litigation_signals: list[LitigationSignalEntry] = []
    current_owner: Optional[str] = None    # most recent buyer or 7/12 occupant
    total_transactions: int = 0            # total IGR rows for this survey
    title_period_years: Optional[int] = None  # years from oldest IGR txn to now
    flagged: bool = False                  # True when any litigation signals exist


class LandCaseWorkflowArtifactsResponse(BaseModel):
    workflow_id: str
    pdf_path: Optional[str]
    html_path: Optional[str]
    ranked_csv_path: Optional[str]


class WorkflowSummaryResponse(BaseModel):
    """Lightweight workflow summary used in list endpoints and sidebar."""
    workflow_id: str
    status: str
    district_label: str
    taluka_label: str
    village_label: str
    survey_part1: str
    survey_option_label: str
    owner_name: Optional[str]
    total_hits: int
    created_at: datetime
    finished_at: Optional[datetime]


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowSummaryResponse]
    total: int
