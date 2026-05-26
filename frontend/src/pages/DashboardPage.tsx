import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiGet, type WorkflowSummary } from '../api/client';
import StatusBadge from '../components/ui/StatusBadge';
import { BRAND_NAME } from '../config/brand';

export default function DashboardPage() {
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<{ workflows: WorkflowSummary[] }>('/workflows')
      .then((d) => setWorkflows(d.workflows))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <>
      <div className="eyebrow">{BRAND_NAME}</div>
      <h1 className="page-title">Dashboard</h1>
      <p className="page-subtitle">Past land title intelligence reports</p>

      {error && <div className="error-banner">{error}</div>}

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
                  <Link
                    to={`/report/workflow/${wf.workflow_id}`}
                    className="btn btn--secondary btn--sm"
                  >
                    View
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
