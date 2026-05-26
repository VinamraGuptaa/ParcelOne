interface Props {
  status: string;
  message?: string | null;
  yearsDone?: number;
  yearsTotal?: number;
}

/** Map workflow status → fixed stage percentage per spec. */
function stagePct(status: string): number {
  const s = status.toLowerCase();
  if (s === 'ranked_done' || s === 'done' || s === 'completed' || s === 'succeeded') return 100;
  if (s.includes('ecourt')) return 75;
  if (s === 'igr_running') return 50;
  if (s === 'bhulekh_running' || s === 'name_variants_ready') return 20;
  return 5;
}

export default function ProgressBar({ status, message, yearsDone, yearsTotal }: Props) {
  const pct = stagePct(status);
  const stepLabel =
    yearsTotal != null && yearsTotal > 0
      ? `Step ${yearsDone ?? 0} of ${yearsTotal}`
      : null;

  return (
    <div className="progress-wrap">
      <div className="progress-label">
        <span>{friendlyStatus(status)}</span>
        {stepLabel && <span>{stepLabel}</span>}
      </div>
      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${pct}%` }} />
      </div>
      {message && <p className="progress-message">{message}</p>}
    </div>
  );
}

function friendlyStatus(status: string): string {
  const s = status.toLowerCase();
  if (s === 'failed') return 'Unable to complete';
  if (s === 'ranked_done' || s === 'done' || s === 'completed' || s === 'succeeded') return 'Completed';
  if (s === 'igr_running') return 'Searching land records';
  if (s.includes('ecourt') || s.includes('rank')) return 'Gathering case records';
  if (s === 'bhulekh_running') return 'Fetching 7/12 extract';
  if (s === 'name_variants_ready') return 'Preparing name variants';
  return 'Preparing your results';
}
