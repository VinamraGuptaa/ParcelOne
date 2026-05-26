import { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { apiGet, API_BASE, type JobResponse, type CaseRow } from '../api/client';
import StatusBadge from '../components/ui/StatusBadge';

const POLL_MS = 3000;

function isDone(status: string): boolean {
  return status === 'done';
}

const COLUMN_LABELS: Record<string, string> = {
  search_year: 'Year',
  sr_no: 'Sr No',
  case_type_number_year: 'Case / Year',
  petitioner_vs_respondent: 'Petitioner vs Respondent',
  cnr_number: 'CNR',
  case_type: 'Case Type',
  filing_number: 'Filing No.',
  filing_date: 'Filed',
  registration_date: 'Reg. Date',
  case_status: 'Status',
  court_number_judge: 'Court',
};

const SHOW_COLS = [
  'search_year', 'case_type_number_year', 'petitioner_vs_respondent',
  'cnr_number', 'case_type', 'filing_date', 'case_status', 'court_number_judge',
] as const;

export default function JobReportPage() {
  const { id } = useParams<{ id: string }>();
  const [job, setJob] = useState<JobResponse | null>(null);
  const [cases, setCases] = useState<CaseRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function stopPolling() {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }

  async function loadCases(jobId: string) {
    try {
      let all: CaseRow[] = [];
      let offset = 0;
      const limit = 200;
      while (true) {
        const data = await apiGet<{ cases: CaseRow[]; total: number }>(
          `/jobs/${jobId}/cases?limit=${limit}&offset=${offset}`
        );
        all = all.concat(data.cases);
        if (all.length >= data.total || data.cases.length < limit) break;
        offset += limit;
      }
      setCases(all);
    } catch (e: unknown) {
      setError((e as Error).message);
    }
  }

  async function poll(jobId: string) {
    try {
      const data = await apiGet<JobResponse>(`/jobs/${jobId}`);
      setJob(data);
      if (isDone(data.status)) {
        stopPolling();
        await loadCases(jobId);
      } else if (data.status === 'failed') {
        stopPolling();
        setError(data.error_message ?? 'Scraping failed.');
      }
    } catch {
      // silently ignore transient errors
    }
  }

  useEffect(() => {
    if (!id) return;
    apiGet<JobResponse>(`/jobs/${id}`)
      .then(async (data) => {
        setJob(data);
        if (isDone(data.status)) {
          await loadCases(id);
        } else if (data.status === 'failed') {
          setError(data.error_message ?? 'Scraping failed.');
        } else {
          timerRef.current = setInterval(() => poll(id), POLL_MS);
        }
      })
      .catch((e: Error) => setError(e.message));

    return stopPolling;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (!job && !error) {
    return (
      <div style={{ padding: '40px 0' }}>
        <div className="skeleton" style={{ height: 28, width: 240, marginBottom: 12 }} />
        <div className="skeleton" style={{ height: 4, width: '100%' }} />
      </div>
    );
  }

  if (error) return <div className="error-banner">{error}</div>;
  if (!job) return null;

  const isRunning = !isDone(job.status) && job.status !== 'failed';

  return (
    <>
      <div className="eyebrow">eCourts Search</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 4 }}>
        <h1 className="page-title" style={{ marginBottom: 0 }}>{job.petitioner_name}</h1>
        <StatusBadge status={job.status} />
      </div>
      <p className="page-subtitle">
        {job.year ? `Year: ${job.year}` : 'All years (last 15)'}
        {' · '}
        {new Date(job.created_at).toLocaleDateString('en-IN', {
          day: 'numeric', month: 'short', year: 'numeric',
        })}
      </p>

      {/* Progress */}
      {isRunning && (
        <div className="progress-wrap">
          <div className="progress-label">
            <span>{job.progress_message || 'Scraping in progress…'}</span>
            {job.years_total != null && job.years_total > 0 && (
              <span>{job.years_done} / {job.years_total} years</span>
            )}
          </div>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${job.progress_pct || 0}%` }} />
          </div>
        </div>
      )}

      {/* Cases table */}
      {isDone(job.status) && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
            <span style={{ fontFamily: 'var(--f-mono)', fontSize: 11, color: 'var(--ghost)' }}>
              {cases.length} case(s) found
            </span>
            {cases.length > 0 && (
              <a
                href={`${API_BASE}/jobs/${id}/cases/export`}
                className="artifact-link"
                target="_blank"
                rel="noopener noreferrer"
              >
                ↓ CSV
              </a>
            )}
          </div>

          {cases.length === 0 ? (
            <div className="empty-state">
              <p className="empty-state__title">No cases found</p>
              <p className="empty-state__body">
                No matching court cases were found for "{job.petitioner_name}".
              </p>
            </div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    {SHOW_COLS.map((col) => (
                      <th key={col}>{COLUMN_LABELS[col] ?? col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {cases.map((row, i) => (
                    <tr key={i}>
                      {SHOW_COLS.map((col) => (
                        <td key={col} className={col === 'petitioner_vs_respondent' ? 'td-name' : 'td-mono'}>
                          {(row as unknown as Record<string, string | null>)[col] ?? ''}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </>
  );
}
