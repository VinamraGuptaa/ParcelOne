import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  apiGet,
  API_BASE,
  type WorkflowResponse,
  type WorkflowResults,
  type WorkflowArtifacts,
  type LitigationSignal,
} from '../api/client';
import ProgressBar from '../components/ui/ProgressBar';
import StatusBadge from '../components/ui/StatusBadge';
import IGRChain from '../components/IGRChain';

const POLL_MS = 3000;
const MAX_FAILURES = 10;

function isTerminal(status: string): boolean {
  const s = status.toLowerCase();
  return s === 'ranked_done' || s === 'done' || s === 'completed' || s === 'succeeded';
}

function formatConfidence(confidence: number | null | undefined): string {
  if (confidence == null) return '—';
  return `${Math.round(confidence * 100)}%`;
}

function reportTitle(wf: WorkflowResponse): string {
  const village = wf.village_label?.trim();
  const survey = wf.survey_option_label?.trim();
  if (village && survey) return `${village} — ${survey}`;
  if (survey) return `Survey No. ${survey}`;
  if (village) return village;
  return 'Survey results';
}

export default function WorkflowReportPage() {
  const { id } = useParams<{ id: string }>();
  const [wf, setWf] = useState<WorkflowResponse | null>(null);
  const [results, setResults] = useState<WorkflowResults | null>(null);
  const [artifacts, setArtifacts] = useState<WorkflowArtifacts | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [resultsLoading, setResultsLoading] = useState(false);
  const failuresRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function stopPolling() {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }

  async function loadFinalResults(workflowId: string) {
    setResultsLoading(true);
    setResultsError(null);
    try {
      const [r, a] = await Promise.all([
        apiGet<WorkflowResults>(`/workflows/${workflowId}/results`),
        apiGet<WorkflowArtifacts>(`/workflows/${workflowId}/artifacts`),
      ]);
      setResults(r);
      setArtifacts(a);
    } catch (e: unknown) {
      setResultsError((e as Error).message);
    } finally {
      setResultsLoading(false);
    }
  }

  async function poll(workflowId: string) {
    try {
      const data = await apiGet<WorkflowResponse>(`/workflows/${workflowId}`);
      failuresRef.current = 0;
      setWf(data);

      if (isTerminal(data.status)) {
        stopPolling();
        await loadFinalResults(workflowId);
      } else if (data.status === 'failed') {
        stopPolling();
        setPageError(data.error_message ?? 'Workflow failed.');
      }
    } catch {
      failuresRef.current += 1;
      if (failuresRef.current >= MAX_FAILURES) {
        stopPolling();
        setPageError('Lost connection to the server. Please refresh the page.');
      }
    }
  }

  useEffect(() => {
    if (!id) return;
    setWf(null);
    setResults(null);
    setArtifacts(null);
    setPageError(null);
    setResultsError(null);
    setResultsLoading(false);

    apiGet<WorkflowResponse>(`/workflows/${id}`)
      .then(async (data) => {
        setWf(data);
        if (isTerminal(data.status)) {
          await loadFinalResults(id);
        } else if (data.status === 'failed') {
          setPageError(data.error_message ?? 'Workflow failed.');
        } else {
          timerRef.current = setInterval(() => poll(id), POLL_MS);
        }
      })
      .catch((e: Error) => setPageError(e.message));

    return stopPolling;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (!wf && !pageError) {
    return (
      <div style={{ padding: '40px 0' }}>
        <div className="skeleton" style={{ height: 28, width: 240, marginBottom: 12 }} />
        <div className="skeleton" style={{ height: 4, width: '100%' }} />
      </div>
    );
  }

  if (pageError && !wf) {
    return <div className="error-banner">{pageError}</div>;
  }

  if (!wf) return null;

  const title = reportTitle(wf);
  const isRunning = !isTerminal(wf.status) && wf.status !== 'failed';
  const isDone = isTerminal(wf.status);

  return (
    <>
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <Link to="/">Dashboard</Link>
        <span className="breadcrumb__sep">›</span>
        <span>Workflow Report</span>
      </nav>

      <div className="eyebrow">
        {[wf.district_label, wf.taluka_label].filter(Boolean).join(' · ')}
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 4 }}>
        <h1 className="page-title" style={{ marginBottom: 0 }}>{title}</h1>
        <StatusBadge status={wf.status} />
      </div>
      <p className="page-subtitle">
        {new Date(wf.created_at).toLocaleDateString('en-IN', {
          day: 'numeric', month: 'long', year: 'numeric',
        })}
        {wf.owner_name ? ` · ${wf.owner_name}` : ''}
      </p>

      {pageError && <div className="error-banner">{pageError}</div>}

      {isRunning && (
        <ProgressBar
          status={wf.status}
          message={wf.progress_message}
          yearsDone={wf.years_done}
          yearsTotal={wf.years_total}
        />
      )}

      {isDone && <PollSummaryStrip wf={wf} />}

      {isDone && resultsLoading && <ReportLoadingSkeleton />}

      {isDone && resultsError && !resultsLoading && (
        <div className="error-banner" style={{ marginBottom: 16 }}>
          Failed to load report details: {resultsError}
          {' '}
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            style={{ marginLeft: 8 }}
            onClick={() => loadFinalResults(wf.workflow_id)}
          >
            Retry
          </button>
        </div>
      )}

      {results && <ReportDetails workflowId={wf.workflow_id} results={results} artifacts={artifacts} />}
    </>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function PollSummaryStrip({ wf }: { wf: WorkflowResponse }) {
  const hits = wf.total_hits ?? 0;
  const yearsLabel =
    wf.years_total != null && wf.years_total > 0
      ? `${wf.years_done ?? 0} / ${wf.years_total}`
      : '—';

  return (
    <div className="metrics-strip">
      <div className="metric-cell">
        <div className="metric-cell__label">Case Hits</div>
        <div className={`metric-cell__value${hits > 0 ? ' metric-cell__value--red' : ''}`}>
          {hits}
        </div>
      </div>
      <div className="metric-cell">
        <div className="metric-cell__label">Years Searched</div>
        <div className="metric-cell__value">{yearsLabel}</div>
      </div>
      <div className="metric-cell">
        <div className="metric-cell__label">Owner (Extracted)</div>
        <div className="metric-cell__value metric-cell__value--name">
          {wf.occupant_primary_name || '—'}
        </div>
      </div>
      <div className="metric-cell">
        <div className="metric-cell__label">Confidence</div>
        <div className="metric-cell__value">{formatConfidence(wf.extraction_confidence)}</div>
      </div>
    </div>
  );
}

function ReportLoadingSkeleton() {
  return (
    <div className="report-loading" aria-busy="true" aria-label="Loading report details">
      <div className="report-loading__label">
        <span className="spinner" />
        Loading report details…
      </div>
      {[1, 2, 3].map((i) => (
        <div key={i} style={{ marginBottom: 20 }}>
          <div className="skeleton" style={{ height: 22, width: 180, marginBottom: 10 }} />
          <div className="skeleton" style={{ height: 12, width: '100%', marginBottom: 6 }} />
          <div className="skeleton" style={{ height: 12, width: '85%' }} />
        </div>
      ))}
    </div>
  );
}

function ReportDetails({
  workflowId,
  results,
  artifacts,
}: {
  workflowId: string;
  results: WorkflowResults;
  artifacts: WorkflowArtifacts | null;
}) {
  return (
    <>
      <ArtifactsRow workflowId={workflowId} artifacts={artifacts} />

      {(results.entity || results.igr_purchaser_names.length > 0) && (
        <section>
          <div className="section-header">
            <span className="section-header__title">Land Record</span>
            <span className="section-header__meta">7/12 Extract</span>
          </div>
          <EntityBlock results={results} />
        </section>
      )}

      <section>
        <div className="section-header">
          <span className="section-header__title">Ownership Timeline</span>
          <span className="section-header__meta">IGR · {results.total_transactions} transaction(s)</span>
        </div>
        <IGRChain transactions={results.ownership_timeline} />
      </section>

      <section>
        <div className="section-header">
          <span className="section-header__title">Litigation Signals</span>
          <span className="section-header__meta">eCourts · {results.litigation_signals.length} signal(s)</span>
        </div>
        <LitigationSection signals={results.litigation_signals} />
      </section>

      {results.ecourts_api_cases.length > 0 && (
        <section>
          <div className="section-header">
            <span className="section-header__title">Case Details</span>
            <span className="section-header__meta">{results.ecourts_api_cases.length} case(s)</span>
          </div>
          <CasesTable cases={results.ecourts_api_cases} />
        </section>
      )}
    </>
  );
}

function ArtifactsRow({
  workflowId,
  artifacts,
}: {
  workflowId: string;
  artifacts: WorkflowArtifacts | null;
}) {
  if (!artifacts) return null;
  const items: [string, string][] = [];
  if (artifacts.pdf_path)        items.push(['pdf', 'Mahabhulekh PDF']);
  if (artifacts.ranked_csv_path) items.push(['csv', 'Ranked CSV']);
  if (artifacts.html_path)       items.push(['html', 'Submitted HTML']);
  if (!items.length) return null;

  return (
    <div className="artifacts-row">
      {items.map(([kind, label]) => (
        <a
          key={kind}
          href={`${API_BASE}/workflows/${workflowId}/artifact/${kind}`}
          target="_blank"
          rel="noopener noreferrer"
          className="artifact-link"
        >
          ↓ {label}
        </a>
      ))}
    </div>
  );
}

function EntityBlock({ results }: { results: WorkflowResults }) {
  const lines: string[] = [];
  if (results.owner_name) lines.push(`Owner input: ${results.owner_name}`);
  if (results.entity?.occupant_primary_name) lines.push(`7/12 occupant: ${results.entity.occupant_primary_name}`);
  if (results.entity?.occupant_candidates.length)
    lines.push(`Other occupants: ${results.entity.occupant_candidates.join(', ')}`);
  if (results.entity?.mutation_numbers.length)
    lines.push(`Mutations: ${results.entity.mutation_numbers.join(', ')}`);
  if (results.igr_purchaser_names.length)
    lines.push(`IGR names found: ${results.igr_purchaser_names.join(', ')}`);

  return (
    <div className="card" style={{ fontFamily: 'var(--f-prose)', fontSize: 15 }}>
      {lines.map((l, i) => {
        const [label, ...rest] = l.split(': ');
        return (
          <p key={i} style={{ marginBottom: 4 }}>
            <strong>{label}:</strong> {rest.join(': ')}
          </p>
        );
      })}
    </div>
  );
}

const RELEVANCE_COLOR: Record<string, string> = {
  high:   'var(--red)',
  medium: 'var(--amber)',
  low:    'var(--ghost)',
};

function LitigationSection({ signals }: { signals: LitigationSignal[] }) {
  if (signals.length === 0) {
    return (
      <div className="empty-state">
        <p className="empty-state__title">No litigation signals</p>
        <p className="empty-state__body">No court cases were found linked to this survey number.</p>
      </div>
    );
  }

  return (
    <div>
      {signals.map((s, i) => {
        const metaParts = [s.case_type, s.court, s.year, s.is_pending ? 'Pending' : s.case_status]
          .filter(Boolean)
          .join(' · ');
        return (
          <div key={i} className="case-hit">
            <div className="case-hit__rank">{s.final_rank ?? '—'}</div>
            <div>
              <div style={{ fontFamily: 'var(--f-prose)', fontSize: 14 }}>{s.parties || '—'}</div>
              <div className="case-hit__cnr">{metaParts}</div>
            </div>
            <div
              className="case-hit__score"
              style={{ color: RELEVANCE_COLOR[s.relevance] ?? 'var(--ghost)' }}
            >
              {s.relevance}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function CasesTable({ cases }: { cases: WorkflowResults['ecourts_api_cases'] }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>CNR</th>
            <th>Case Type</th>
            <th>Parties</th>
            <th>Court</th>
            <th>Status</th>
            <th>Filed</th>
          </tr>
        </thead>
        <tbody>
          {cases.map((c, i) => (
            <tr key={i}>
              <td className="td-mono">{c.final_rank ?? '—'}</td>
              <td className="td-mono">{c.cnr_number || '—'}</td>
              <td className="td-mono">{c.case_type || c.case_type_raw || '—'}</td>
              <td className="td-name" style={{ maxWidth: 280 }}>
                {c.petitioners.slice(0, 1).join('') || ''}
                {c.petitioners.length > 0 && c.respondents.length > 0 ? ' v. ' : ''}
                {c.respondents.slice(0, 1).join('') || ''}
              </td>
              <td className="td-mono">{c.court || '—'}</td>
              <td>
                {c.is_pending ? (
                  <span className="badge badge--running">Pending</span>
                ) : (
                  <span className="badge badge--done">Closed</span>
                )}
              </td>
              <td className="td-mono">{c.filing_date || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
