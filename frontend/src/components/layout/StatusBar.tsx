import { useEffect, useState } from 'react';
import { API_BASE } from '../../api/client';
import { BRAND_NAME } from '../../config/brand';

type DotColor = 'green' | 'amber' | 'red';

export default function StatusBar() {
  const [dotColor, setDotColor] = useState<DotColor>('green');
  const [statusText, setStatusText] = useState('Ready');

  useEffect(() => {
    let cancelled = false;

    async function ping() {
      try {
        const res = await fetch(`${API_BASE}/health`, {
          signal: AbortSignal.timeout(5000),
        });
        if (cancelled) return;
        if (res.ok) {
          setDotColor('green');
          setStatusText('Ready');
        } else {
          setDotColor('amber');
          setStatusText('Degraded');
        }
      } catch {
        if (!cancelled) {
          setDotColor('red');
          setStatusText('Offline');
        }
      }
    }

    ping();
    const timer = setInterval(ping, 30_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <footer className="statusbar">
      <span className={`statusbar__dot${dotColor !== 'green' ? ` statusbar__dot--${dotColor}` : ''}`} />
      <span className="statusbar__text">{statusText}</span>
      <span className="statusbar__version">{BRAND_NAME}</span>
    </footer>
  );
}
