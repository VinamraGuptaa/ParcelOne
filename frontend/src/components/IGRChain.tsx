import { useState } from 'react';
import type { IgrTransaction } from '../api/client';

const VISIBLE_DEFAULT = 3;

interface Props {
  transactions: IgrTransaction[];
  loading?: boolean;
}

export default function IGRChain({ transactions, loading }: Props) {
  const [expanded, setExpanded] = useState(false);

  if (loading) {
    return (
      <div style={{ padding: '16px 0' }}>
        {[120, 90, 100].map((w, i) => (
          <div key={i} style={{ display: 'flex', gap: 12, marginBottom: 16, alignItems: 'flex-start' }}>
            <div
              className="skeleton"
              style={{ width: 10, height: 10, borderRadius: '50%', flexShrink: 0, marginTop: 3 }}
            />
            <div style={{ flex: 1 }}>
              <div className="skeleton" style={{ height: 12, width: `${w}px`, marginBottom: 6 }} />
              <div className="skeleton" style={{ height: 10, width: '60%' }} />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (transactions.length === 0) {
    return (
      <div className="empty-state">
        <p className="empty-state__title" style={{ fontFamily: 'var(--f-display)', fontSize: 18 }}>
          No IGR records found
        </p>
        <p className="empty-state__body">No property registrations were found for this survey number.</p>
      </div>
    );
  }

  const visible = expanded ? transactions : transactions.slice(0, VISIBLE_DEFAULT);
  const hiddenCount = transactions.length - VISIBLE_DEFAULT;

  return (
    <div style={{ paddingTop: 4 }}>
      {visible.map((t, i) => (
        <TimelineItem key={i} txn={t} />
      ))}
      {!expanded && hiddenCount > 0 && (
        <button
          className="btn btn--secondary btn--sm"
          style={{ marginTop: 8 }}
          onClick={() => setExpanded(true)}
        >
          {hiddenCount} earlier {hiddenCount === 1 ? 'entry' : 'entries'} ▾
        </button>
      )}
    </div>
  );
}

function TimelineItem({ txn: t }: { txn: IgrTransaction }) {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '16px 1fr auto',
        gap: '0 12px',
        marginBottom: 16,
        alignItems: 'flex-start',
      }}
    >
      {/* dot + line */}
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        <div
          style={{
            width: 10,
            height: 10,
            borderRadius: '50%',
            background: 'var(--brass)',
            border: '2px solid var(--newsprint)',
            flexShrink: 0,
            marginTop: 3,
          }}
        />
        <div style={{ flex: 1, width: 1, background: 'var(--rule)', minHeight: 20 }} />
      </div>

      {/* body */}
      <div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
          <span style={{ fontFamily: 'var(--f-display)', fontSize: 17, color: 'var(--ink)' }}>
            {t.doc_type || t.doc_type_marathi || '—'}
          </span>
          {t.litigation_linked && (
            <span className="badge badge--failed" style={{ fontSize: 8 }}>
              Litigation linked
            </span>
          )}
        </div>
        <div style={{ fontFamily: 'var(--f-prose)', fontSize: 14, color: 'var(--ink)', marginTop: 2 }}>
          {t.seller && t.buyer
            ? <>{t.seller} <span style={{ color: 'var(--ghost)' }}>→</span> {t.buyer}</>
            : t.buyer || t.seller || '—'}
        </div>
        <div style={{ fontFamily: 'var(--f-mono)', fontSize: 10, color: 'var(--ghost)', marginTop: 3 }}>
          {[t.doc_no && `Doc No. ${t.doc_no}`, t.sro_name].filter(Boolean).join(' · ')}
        </div>
        {t.seller && t.buyer && (
          <div
            style={{
              fontFamily: 'var(--f-mono)',
              fontSize: 10,
              color: 'var(--amber)',
              marginTop: 2,
            }}
          >
            Consideration
          </div>
        )}
      </div>

      {/* date */}
      <div style={{ fontFamily: 'var(--f-mono)', fontSize: 10, color: 'var(--ghost)', whiteSpace: 'nowrap', marginTop: 4 }}>
        {t.reg_date_fmt || t.year || ''}
      </div>
    </div>
  );
}
