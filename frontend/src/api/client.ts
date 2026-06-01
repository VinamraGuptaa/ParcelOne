export const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api';

const SESSION_TOKEN_KEY = 'plotwise_session_token';

export function getSessionToken(): string | null {
  try {
    return sessionStorage.getItem(SESSION_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setSessionToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(SESSION_TOKEN_KEY, token);
    else sessionStorage.removeItem(SESSION_TOKEN_KEY);
  } catch {
    /* private browsing */
  }
}

function authHeaders(): Record<string, string> {
  const token = getSessionToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// ── Generic helpers ──────────────────────────────────────────────────────────

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: authHeaders(),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({})) as { detail?: string };
    throw new Error(data.detail ?? `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    credentials: 'include',
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({})) as { detail?: unknown };
    const detail = data.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : typeof detail === 'object' && detail !== null && 'message' in detail
          ? String((detail as { message: string }).message)
          : JSON.stringify(detail) ?? `HTTP ${res.status}`;
    throw Object.assign(new Error(message), { status: res.status, detail });
  }
  return res.json() as Promise<T>;
}

// ── Types ────────────────────────────────────────────────────────────────────

export interface WorkflowSummary {
  workflow_id: string;
  status: string;
  district_label: string;
  taluka_label: string;
  village_label: string;
  survey_part1: string;
  survey_option_label: string;
  owner_name: string | null;
  total_hits: number;
  created_at: string;
  finished_at: string | null;
}

export interface WorkflowResponse {
  workflow_id: string;
  status: string;
  progress_message: string | null;
  error_message: string | null;
  district_label: string;
  taluka_label: string;
  village_label: string;
  survey_part1: string;
  survey_option_label: string;
  owner_name: string | null;
  occupant_primary_name: string | null;
  years_total: number;
  years_done: number;
  total_hits: number;
  progress_pct: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface IgrTransaction {
  doc_no: string;
  doc_type: string;
  doc_type_marathi: string;
  reg_date: string;
  reg_date_fmt: string;
  sro_name: string;
  seller: string;
  buyer: string;
  year: string | null;
  litigation_linked: boolean;
}

export interface LitigationSignal {
  parties: string;
  case_type: string | null;
  court: string | null;
  year: string | null;
  cnr_number: string | null;
  case_status: string | null;
  is_pending: boolean;
  relevance: 'high' | 'medium' | 'low';
  final_rank: number | null;
}

export interface EcourtsApiCase {
  cnr_number: string;
  case_type: string | null;
  case_type_raw: string | null;
  case_status: string | null;
  court: string | null;
  court_no: string | null;
  district: string | null;
  state: string | null;
  case_number: string | null;
  cnr_year: string | null;
  filing_number: string | null;
  filing_date: string | null;
  registration_number: string | null;
  registration_date: string | null;
  first_hearing_date: string | null;
  next_hearing_date: string | null;
  decision_date: string | null;
  petitioners: string[];
  respondents: string[];
  petitioner_advocates: string[];
  respondent_advocates: string[];
  case_category_facet_path: string | null;
  parties_text: string | null;
  is_civil: boolean;
  is_pending: boolean;
  final_rank: number | null;
  source_stage: string | null;
}

export interface LandEntity {
  occupant_primary_name: string | null;
  occupant_candidates: string[];
  mutation_numbers: string[];
}

export interface WorkflowResults {
  workflow_id: string;
  district_label: string;
  taluka_label: string;
  village_label: string;
  survey_option_label: string | null;
  owner_name: string | null;
  entity: LandEntity | null;
  variants: Array<{ variant_text: string; variant_kind: string; quality_score: number }>;
  survey_options: string[];
  hits: Array<{
    search_year: string | null;
    case_id: string;
    cnr_number: string | null;
    case_type: string | null;
    court: string | null;
    parties_text: string | null;
    is_civil: boolean;
    name_match_score: number | null;
    matched_variant: string | null;
    match_explanation: string | null;
    final_rank: number | null;
  }>;
  igr_hits: Array<{ survey_number: string; search_year: string; raw: Record<string, unknown> }>;
  igr_purchaser_names: string[];
  total_hits: number;
  ownership_timeline: IgrTransaction[];
  litigation_signals: LitigationSignal[];
  current_owner: string | null;
  total_transactions: number;
  title_period_years: number | null;
  flagged: boolean;
  ecourts_api_cases: EcourtsApiCase[];
}

export interface WorkflowArtifacts {
  workflow_id: string;
  pdf_path: string | null;
  html_path: string | null;
  ranked_csv_path: string | null;
}

// ── Jobs (eCourts name search) ───────────────────────────────────────────────

export interface JobResponse {
  job_id: string;
  petitioner_name: string;
  year: string | null;
  status: string;
  progress_message: string | null;
  error_message: string | null;
  years_total: number | null;
  years_done: number;
  total_cases: number;
  progress_pct: number;
  created_at: string;
}

export interface CaseRow {
  id: string;
  search_year: string | null;
  sr_no: string | null;
  case_type_number_year: string | null;
  petitioner_vs_respondent: string | null;
  cnr_number: string | null;
  case_type: string | null;
  filing_number: string | null;
  filing_date: string | null;
  registration_number: string | null;
  registration_date: string | null;
  first_hearing_date: string | null;
  next_hearing_date: string | null;
  case_stage: string | null;
  decision_date: string | null;
  case_status: string | null;
  nature_of_disposal: string | null;
  court_number_judge: string | null;
  petitioner_and_advocate: string | null;
  respondent_and_advocate: string | null;
  under_acts: string | null;
}

// ── Bhulekh catalog ──────────────────────────────────────────────────────────

export interface Village {
  value: string;
  label: string;
  english?: string;
}

export interface Taluka {
  value: string;
  label: string;
  english?: string;
  villages: Village[];
}

export interface District {
  value: string;
  label: string;
  english?: string;
  talukas: Taluka[];
}

export interface BhulekhCatalog {
  generated_at: string;
  districts: District[];
}
