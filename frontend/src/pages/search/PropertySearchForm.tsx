import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { apiPost, type BhulekhCatalog, type District, type Taluka, type WorkflowResponse } from '../../api/client';

const CATALOG_URL = '/data/bhulekh_catalog.json';

function formatLabel(item: { label?: string; english?: string }): string {
  const lab = (item.label ?? '').trim();
  const eng = (item.english ?? '').trim();
  if (!lab) return eng || '(unnamed)';
  if (!eng || eng.toLowerCase() === lab.toLowerCase()) return lab;
  return `${lab} (${eng})`;
}

export default function PropertySearchForm() {
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<BhulekhCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  const [districtIdx, setDistrictIdx] = useState('');
  const [talukaIdx, setTalukaIdx] = useState('');
  const [villageLabel, setVillageLabel] = useState('');
  const [surveyPart1, setSurveyPart1] = useState('');
  const [surveyOption, setSurveyOption] = useState('');
  const [ownerName, setOwnerName] = useState('');

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeWorkflowId, setActiveWorkflowId] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    fetch(CATALOG_URL, { cache: 'no-cache' })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<BhulekhCatalog>;
      })
      .then(setCatalog)
      .catch((e: Error) =>
        setCatalogError(`Could not load dropdown catalog: ${e.message}. Run scripts/build_bhulekh_catalog.py to rebuild it.`)
      );
    return () => abortRef.current?.abort();
  }, []);

  const districts: District[] = catalog?.districts ?? [];
  const selectedDistrict: District | null = districtIdx !== '' ? (districts[Number(districtIdx)] ?? null) : null;
  const talukas: Taluka[] = selectedDistrict?.talukas ?? [];
  const selectedTaluka: Taluka | null = talukaIdx !== '' ? (talukas[Number(talukaIdx)] ?? null) : null;
  const villages = selectedTaluka?.villages ?? [];

  function onDistrictChange(e: React.ChangeEvent<HTMLSelectElement>) {
    setDistrictIdx(e.target.value);
    setTalukaIdx('');
    setVillageLabel('');
  }

  function onTalukaChange(e: React.ChangeEvent<HTMLSelectElement>) {
    setTalukaIdx(e.target.value);
    setVillageLabel('');
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setActiveWorkflowId(null);

    if (!selectedDistrict || !selectedTaluka || !villageLabel.trim()) {
      setError('Select a district, taluka, and village.');
      return;
    }
    if (!surveyPart1.trim() || !surveyOption.trim()) {
      setError('Fill in the survey base and target survey label.');
      return;
    }

    setSubmitting(true);
    try {
      const wf = await apiPost<WorkflowResponse>('/workflows/land-case-search', {
        district_label: selectedDistrict.label,
        taluka_label: selectedTaluka.label,
        village_label: villageLabel.trim(),
        survey_part1: surveyPart1.trim(),
        survey_option_label: surveyOption.trim(),
        owner_name: ownerName.trim() || null,
      });
      window.dispatchEvent(new Event('plotwise:refresh-sidebar'));
      navigate(`/report/workflow/${wf.workflow_id}`);
    } catch (err: unknown) {
      const e = err as Error & { status?: number; detail?: unknown };
      if (e.status === 409) {
        setError('Another land workflow is already in progress. View it below or wait for it to finish.');
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
        setError(e.message ?? 'Submission failed.');
      }
      setSubmitting(false);
    }
  }

  if (catalogError) {
    return <div className="error-banner">{catalogError}</div>;
  }

  return (
    <form onSubmit={handleSubmit} autoComplete="off">
      {error && (
        <div className="error-banner">
          {error}
          {activeWorkflowId && (
            <div className="mt-8">
              <Link to={`/report/workflow/${activeWorkflowId}`} className="btn btn--secondary btn--sm">
                View running report
              </Link>
            </div>
          )}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16 }}>
        <div className="form-field">
          <label className="field-label">District</label>
          <select
            value={districtIdx}
            onChange={onDistrictChange}
            required
            disabled={submitting || !catalog}
          >
            <option value="">{catalog ? 'Select district' : 'Loading…'}</option>
            {districts.map((d, i) => (
              <option key={i} value={String(i)}>
                {formatLabel(d)}
              </option>
            ))}
          </select>
        </div>

        <div className="form-field">
          <label className="field-label">Taluka</label>
          <select
            value={talukaIdx}
            onChange={onTalukaChange}
            required
            disabled={submitting || !selectedDistrict}
          >
            <option value="">{selectedDistrict ? 'Select taluka' : 'Pick a district first'}</option>
            {talukas.map((t, i) => (
              <option key={i} value={String(i)}>
                {formatLabel(t)}
              </option>
            ))}
          </select>
        </div>

        <div className="form-field">
          <label className="field-label">Village</label>
          <select
            value={villageLabel}
            onChange={(e) => setVillageLabel(e.target.value)}
            required
            disabled={submitting || !selectedTaluka}
          >
            <option value="">{selectedTaluka ? 'Select village' : 'Pick a taluka first'}</option>
            {villages.map((v) => (
              <option key={v.value} value={v.label.trim()}>
                {formatLabel(v)}
              </option>
            ))}
          </select>
        </div>

        <div className="form-field">
          <label className="field-label">Survey Base (Part 1)</label>
          <input
            type="text"
            placeholder="e.g. 70"
            value={surveyPart1}
            onChange={(e) => setSurveyPart1(e.target.value)}
            required
            disabled={submitting}
          />
        </div>

        <div className="form-field">
          <label className="field-label">Target Survey Label</label>
          <input
            type="text"
            placeholder="e.g. 70/6"
            value={surveyOption}
            onChange={(e) => setSurveyOption(e.target.value)}
            required
            disabled={submitting}
          />
        </div>

        <div className="form-field">
          <label className="field-label">
            Owner Name <span className="text-ghost">(optional)</span>
          </label>
          <input
            type="text"
            placeholder="Auto-extracted from 7/12 if blank"
            value={ownerName}
            onChange={(e) => setOwnerName(e.target.value)}
            disabled={submitting}
          />
          <span className="field-hint">Leave blank to auto-extract from Bhulekh</span>
        </div>
      </div>

      <div className="mt-16">
        <button type="submit" className="btn btn--primary" disabled={submitting || !catalog}>
          {submitting ? (
            <>
              <span className="spinner" />
              Running…
            </>
          ) : (
            'Run Land → Cases'
          )}
        </button>
      </div>

      {catalog && (
        <p className="mt-8" style={{ fontSize: 10, color: 'var(--ghost)' }}>
          Catalog: {catalog.districts.length} district(s) · generated {catalog.generated_at}
        </p>
      )}
    </form>
  );
}
