import { Link, useParams } from 'react-router-dom';
import type { WorkflowSummary } from '../../api/client';

interface Props {
  workflows: WorkflowSummary[];
}

export default function Sidebar({ workflows }: Props) {
  const { id } = useParams<{ id: string }>();

  return (
    <aside className="sidebar">
      <div className="sidebar__section-label">Recent Searches</div>
      {workflows.length === 0 ? (
        <p className="sidebar__empty">No searches yet</p>
      ) : (
        workflows.map((wf) => (
          <Link
            key={wf.workflow_id}
            to={`/report/workflow/${wf.workflow_id}`}
            className={
              'sidebar__item' + (id === wf.workflow_id ? ' sidebar__item--active' : '')
            }
          >
            <span className="sidebar__item-label">
              {wf.survey_option_label
                ? `Survey ${wf.survey_option_label}`
                : wf.village_label || wf.workflow_id}
            </span>
            <span className="sidebar__item-meta">{wf.village_label}</span>
          </Link>
        ))
      )}
    </aside>
  );
}
