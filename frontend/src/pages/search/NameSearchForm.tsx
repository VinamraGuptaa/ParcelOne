import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiPost, type JobResponse } from '../../api/client';

export default function NameSearchForm() {
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [year, setYear] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (name.trim().length < 3) {
      setError('Petitioner name must be at least 3 characters.');
      return;
    }
    if (year && !/^\d{4}$/.test(year)) {
      setError('Year must be a 4-digit number.');
      return;
    }

    setSubmitting(true);
    try {
      const job = await apiPost<JobResponse>('/jobs', {
        petitioner_name: name.trim(),
        year: year || null,
      });
      navigate(`/report/job/${job.job_id}`);
    } catch (err: unknown) {
      setError((err as Error).message ?? 'Submission failed.');
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} autoComplete="off">
      {error && <div className="error-banner">{error}</div>}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 200px', gap: 16 }}>
        <div className="form-field">
          <label className="field-label">Petitioner / Respondent Name</label>
          <input
            type="text"
            placeholder="e.g. Rajesh Gupta"
            value={name}
            onChange={(e) => setName(e.target.value)}
            minLength={3}
            required
            disabled={submitting}
          />
          <span className="field-hint">Searches across all years unless a year is specified</span>
        </div>

        <div className="form-field">
          <label className="field-label">Year <span className="text-ghost">(optional)</span></label>
          <input
            type="text"
            placeholder="e.g. 2019"
            value={year}
            onChange={(e) => setYear(e.target.value)}
            pattern="\d{4}"
            disabled={submitting}
          />
        </div>
      </div>

      <div className="mt-16">
        <button type="submit" className="btn btn--primary" disabled={submitting}>
          {submitting ? (
            <>
              <span className="spinner" />
              Searching…
            </>
          ) : (
            'Search Cases'
          )}
        </button>
      </div>
    </form>
  );
}
