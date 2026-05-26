import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { apiGet, apiPost, type WorkflowResponse, type WorkflowSummary } from '../api/client';
import StatusBadge from '../components/ui/StatusBadge';
import { BRAND_NAME } from '../config/brand';

function isFailed(status: string): boolean {
  return status.toLowerCase() === 'failed';
}

export default function DashboardPage() {
  const navigate = useNavigate();
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [activeWorkflowId, setActiveWorkflowId] = useState<string | null>(null);

  useEffect(() => {
    apiGet<{ workflows: WorkflowSummary[] }>('/workflows')
      .then((d) => setWorkflows(d.workflows))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  async function handleRetry(wf: WorkflowSummary) {
    setRetryError(null);
    setActiveWorkflowId(null);
    setRetryingId(wf.workflow_id);
    try {
      const created = await apiPost<WorkflowResponse>('/workflows/land-case-search', {
        district_label: wf.district_label,
        taluka_label: wf.taluka_label,
        village_label: wf.village_label,
        survey_part1: wf.survey_part1,
        survey_option_label: wf.survey_option_label,
        owner_name: wf.owner_name,
      });
      window.dispatchEvent(new Event('plotwise:refresh-sidebar'));
      navigate(`/report/workflow/${created.workflow_id}`);
    } catch (err: unknown) {
      const e = err as Error & { status?: number; detail?: unknown };
      if (e.status === 409) {
        setRetryError('Another land workflow is already in progress. Wait for it to finish or view it below.');
        const detail = e.detail;
        if (
          typeof detail === 'object' &&
          detail !== null &&
          'active_workflow_id' in detail &&
          typeof (detail as { active_workflow_id: unknown }).active_workflow_id === 'string'
        ) {
          setActiveWorkflowId((detail as { active_workflow_id: string }).active_workflow_id);
        }
      } else {
        setRetryError(e.message ?? 'Retry failed.');
      }
    } finally {
      setRetryingId(null);
    }
  }

  return (
    <>
      <div className="eyebrow">{BRAND_NAME}</div>
      <h1 className="page-title">Dashboard</h1>
      <p className="page-subtitle">Past land title intelligence reports</p>

      {error && <div className="error-banner">{error}</div>}
      {retryError && (
        <div className="error-banner">
          {retryError}
          {activeWorkflowId && (
            <div className="mt-8">
              <Link to={`/report/workflow/${activeWorkflowId}`} className="btn btn--secondary btn--sm">
                View running report
              </Link>
            </div>
          )}
        </div>
      )}

      {loading ? (
        <div style={{ paddingTop: 24 }}>
          {[1, 2, 3].map((i) => (
            <div key={i} className="skeleton" style={{ height: 48, marginBottom: 8, borderRadius: 2 }} />
          ))}
        </div>
      ) : workflows.length === 0 ? (
        <div className="empty-state">
          <p className="empty-state__title">No reports yet</p>
          <p className="empty-state__body">
            Run a search from{' '}
            <Link to="/search" style={{ color: 'var(--red)' }}>
              New Search
            </Link>{' '}
            to generate a land title intelligence report.
          </p>
        </div>
      ) : (
        <table className="data-table w-full">
          <thead>
            <tr>
              <th>Survey</th>
              <th>Village</th>
              <th>Taluka</th>
              <th>Status</th>
              <th>Hits</th>
              <th>Date</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {workflows.map((wf) => (
              <tr key={wf.workflow_id}>
                <td className="td-name">{wf.survey_option_label || wf.survey_part1 || '—'}</td>
                <td className="td-mono">{wf.village_label}</td>
                <td className="td-mono">{wf.taluka_label}</td>
                <td>
                  <StatusBadge status={wf.status} />
                </td>
                <td className="td-mono">{wf.total_hits > 0 ? wf.total_hits : '—'}</td>
                <td className="td-mono">
                  {new Date(wf.created_at).toLocaleDateString('en-IN', {
                    day: 'numeric',
                    month: 'short',
                    year: 'numeric',
                  })}
                </td>
                <td className="td-action">
                  <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
                    {isFailed(wf.status) && (
                      <button
                        type="button"
                        className="btn btn--primary btn--sm"
                        disabled={retryingId !== null}
                        onClick={() => handleRetry(wf)}
                      >
                        {retryingId === wf.workflow_id ? (
                          <>
                            <span className="spinner" />
                            Retrying…
                          </>
                        ) : (
                          'Try again'
                        )}
                      </button>
                    )}
                    <Link
                      to={`/report/workflow/${wf.workflow_id}`}
                      className="btn btn--secondary btn--sm"
                    >
                      View
                    </Link>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
