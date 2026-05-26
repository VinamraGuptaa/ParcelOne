interface Props {
  status: string;
}

function statusClass(status: string): string {
  const s = status.toLowerCase();
  if (s === 'done' || s === 'ranked_done' || s === 'completed' || s === 'succeeded') return 'badge--done';
  if (s === 'failed') return 'badge--failed';
  if (s === 'running' || s.includes('running')) return 'badge--running';
  return 'badge--pending';
}

function statusLabel(status: string): string {
  const s = status.toLowerCase();
  if (s === 'ranked_done') return 'RANKED_DONE';
  if (s.includes('running')) return 'Running';
  if (s === 'pending_input') return 'Pending';
  return status.replace(/_/g, ' ');
}

export default function StatusBadge({ status }: Props) {
  return (
    <span className={`badge ${statusClass(status)}`}>
      {statusLabel(status)}
    </span>
  );
}
