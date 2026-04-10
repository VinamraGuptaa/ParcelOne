/**
 * eCourts India Case Scraper — Frontend
 *
 * API_BASE: set to "/api" for same-origin (local dev / Render serving static).
 * When the frontend is deployed on Netlify/Vercel, update this to the full
 * Render backend URL, e.g. "https://ecourts-api.onrender.com/api"
 */
const API_BASE = "/api";
const POLL_INTERVAL_MS = 3000;

// ── State ──────────────────────────────────────────────────────────────────
let currentJobId = null;
let pollTimer = null;

// ── DOM refs ────────────────────────────────────────────────────────────────
const searchForm     = document.getElementById("search-form");
const nameInput      = document.getElementById("petitioner-name");
const yearInput      = document.getElementById("year");
const submitBtn      = document.getElementById("submit-btn");
const errorBanner    = document.getElementById("error-banner");
const errorText      = document.getElementById("error-text");
const wakeBanner     = document.getElementById("wake-banner");
const progressSection = document.getElementById("progress-section");
const progressFill   = document.getElementById("progress-fill");
const progressPctLabel = document.getElementById("progress-pct-label");
const progressYearsLabel = document.getElementById("progress-years-label");
const progressMsg    = document.getElementById("progress-msg");
const resultsSection = document.getElementById("results-section");
const resultsTitle   = document.getElementById("results-title");
const casesTable     = document.getElementById("cases-table");
const historySection = document.getElementById("history-section");
const historyList    = document.getElementById("history-list");

// ── Init ────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  checkServerHealth();
  loadPastSearches();
  searchForm.addEventListener("submit", submitSearch);
  reattachRunningJob();
});

// ── Re-attach to in-progress job on page load ────────────────────────────
async function reattachRunningJob() {
  const running = await getRunningJob();
  if (!running) return;
  currentJobId = running.job_id;
  setFormDisabled(true);
  showProgress(
    running.progress_message || "Scraping in progress...",
    running.progress_pct || 0,
    running.years_done,
    running.years_total,
  );
  startPolling(currentJobId);
}

// ── Server health (handles Render cold start) ────────────────────────────
async function checkServerHealth() {
  const start = Date.now();
  try {
    const res = await fetch(`${API_BASE}/health`, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) showWakeBanner();
  } catch {
    showWakeBanner();
    // Keep pinging until server is up
    const timer = setInterval(async () => {
      try {
        const r = await fetch(`${API_BASE}/health`, { signal: AbortSignal.timeout(5000) });
        if (r.ok) {
          hideWakeBanner();
          clearInterval(timer);
        }
      } catch { /* still sleeping */ }
    }, 5000);
  }
}

function showWakeBanner() { wakeBanner.style.display = "flex"; }
function hideWakeBanner()  { wakeBanner.style.display = "none"; }

// ── Busy dialog ──────────────────────────────────────────────────────────
const busyOverlay = document.getElementById("busy-overlay");
const busyMsg     = document.getElementById("busy-msg");

function showBusyDialog(runningJob) {
  busyMsg.textContent = `"${runningJob.petitioner_name}" is currently being scraped`
    + (runningJob.progress_message ? ` — ${runningJob.progress_message}` : "")
    + ". Please wait for it to finish before starting a new search.";
  busyOverlay.style.display = "flex";
}

function hideBusyDialog() {
  busyOverlay.style.display = "none";
}

async function getRunningJob() {
  try {
    const res = await fetch(`${API_BASE}/jobs?limit=20`);
    if (!res.ok) return null;
    const data = await res.json();
    return data.jobs.find(j => j.status === "running") || null;
  } catch { return null; }
}

// ── Search form ──────────────────────────────────────────────────────────
async function submitSearch(e) {
  e.preventDefault();
  hideError();

  const running = await getRunningJob();
  if (running) {
    showBusyDialog(running);
    return;
  }

  const name = nameInput.value.trim();
  const year = yearInput.value.trim();

  if (name.length < 3) {
    showError("Petitioner name must be at least 3 characters.");
    return;
  }
  if (year && (!/^\d{4}$/.test(year))) {
    showError("Year must be a 4-digit number.");
    return;
  }

  setFormDisabled(true);
  hideResults();

  try {
    const res = await fetch(`${API_BASE}/jobs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ petitioner_name: name, year: year || null }),
    });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Server error ${res.status}`);
    }

    const job = await res.json();
    currentJobId = job.job_id;
    showProgress("Starting scrape...", 0, null, null);
    startPolling(currentJobId);

  } catch (err) {
    showError(err.message);
    setFormDisabled(false);
  }
}

// ── Polling ──────────────────────────────────────────────────────────────
function startPolling(jobId) {
  clearPolling();
  pollTimer = setInterval(() => pollJobStatus(jobId), POLL_INTERVAL_MS);
  pollJobStatus(jobId); // immediate first check
}

function clearPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function pollJobStatus(jobId) {
  try {
    const res = await fetch(`${API_BASE}/jobs/${jobId}`);
    if (!res.ok) return;
    const job = await res.json();

    showProgress(
      job.progress_message || job.status,
      job.progress_pct || 0,
      job.years_done,
      job.years_total,
    );

    if (job.status === "done") {
      clearPolling();
      await loadCases(jobId, job.total_cases);
      loadPastSearches();
    } else if (job.status === "failed") {
      clearPolling();
      hideProgress();
      showError(job.error_message || "Scraping failed. Please try again.");
      setFormDisabled(false);
      loadPastSearches();
    }
  } catch (err) {
    console.error("Poll error:", err);
  }
}

// ── Load and render cases ────────────────────────────────────────────────
async function loadCases(jobId, totalCount) {
  try {
    // Fetch all cases (up to 1000; paginate if needed)
    let allCases = [];
    let offset = 0;
    const limit = 200;
    while (true) {
      const res = await fetch(`${API_BASE}/jobs/${jobId}/cases?limit=${limit}&offset=${offset}`);
      if (!res.ok) break;
      const data = await res.json();
      allCases = allCases.concat(data.cases);
      if (allCases.length >= data.total || data.cases.length < limit) break;
      offset += limit;
    }

    hideProgress();
    renderTable(allCases);
    resultsTitle.textContent = `Found ${allCases.length} case(s)`;
    resultsSection.style.display = "block";
    setFormDisabled(false);
    resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });

  } catch (err) {
    hideProgress();
    showError("Failed to load results: " + err.message);
    setFormDisabled(false);
  }
}

// ── Table renderer ───────────────────────────────────────────────────────
const COLUMN_LABELS = {
  sr_no: "Sr No",
  case_type_number_year: "Case / Year",
  petitioner_vs_respondent: "Petitioner vs Respondent",
  cnr_number: "CNR Number",
  case_type: "Case Type",
  filing_number: "Filing No.",
  filing_date: "Filing Date",
  registration_number: "Reg. No.",
  registration_date: "Reg. Date",
  first_hearing_date: "First Hearing",
  next_hearing_date: "Next Hearing",
  case_stage: "Case Stage",
  decision_date: "Decision Date",
  case_status: "Status",
  nature_of_disposal: "Disposal",
  court_number_judge: "Court / Judge",
  petitioner_and_advocate: "Petitioner & Advocate",
  respondent_and_advocate: "Respondent & Advocate",
  under_acts: "Under Acts",
  search_year: "Year",
};

// Columns to show (in order); skip internal/noisy ones
const SHOW_COLUMNS = [
  "search_year", "sr_no", "case_type_number_year", "petitioner_vs_respondent",
  "cnr_number", "case_type", "filing_number", "filing_date",
  "registration_number", "registration_date", "first_hearing_date",
  "next_hearing_date", "case_stage", "decision_date", "case_status",
  "nature_of_disposal", "court_number_judge",
  "petitioner_and_advocate", "respondent_and_advocate", "under_acts",
];

function renderTable(cases) {
  if (!cases || cases.length === 0) {
    casesTable.innerHTML = "<thead><tr><th>No records found</th></tr></thead><tbody></tbody>";
    return;
  }

  // Determine which columns actually have data
  const activeCols = SHOW_COLUMNS.filter(col =>
    cases.some(c => c[col] && String(c[col]).trim())
  );

  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  activeCols.forEach(col => {
    const th = document.createElement("th");
    th.textContent = COLUMN_LABELS[col] || col;
    headerRow.appendChild(th);
  });
  thead.appendChild(headerRow);

  const tbody = document.createElement("tbody");
  cases.forEach(row => {
    const tr = document.createElement("tr");
    activeCols.forEach(col => {
      const td = document.createElement("td");
      td.textContent = row[col] != null ? String(row[col]) : "";
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });

  casesTable.innerHTML = "";
  casesTable.appendChild(thead);
  casesTable.appendChild(tbody);
}

// ── CSV export ───────────────────────────────────────────────────────────
function downloadCSV() {
  if (!currentJobId) return;
  const countEl = document.getElementById("results-title");
  if (countEl && countEl.textContent.startsWith("Found 0")) return;
  window.location.href = `${API_BASE}/jobs/${currentJobId}/cases/export`;
}

// ── Past searches ────────────────────────────────────────────────────────
async function loadPastSearches() {
  try {
    const res = await fetch(`${API_BASE}/jobs?limit=10`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data.jobs || data.jobs.length === 0) return;

    historyList.innerHTML = "";
    data.jobs.forEach(job => {
      const item = document.createElement("div");
      item.className = "history-item";

      const meta = document.createElement("div");
      meta.className = "history-meta";
      meta.innerHTML = `
        <span class="history-name">${escHtml(job.petitioner_name)}</span>
        <span>${escHtml(job.year || "Last 15 years")}</span>
        <span class="chip chip-${job.status}">${job.status}</span>
        ${job.total_cases ? `<span>${job.total_cases} case(s)</span>` : ""}
      `;

      const actions = document.createElement("div");
      if (job.status === "done" && job.total_cases > 0) {
        const viewBtn = document.createElement("button");
        viewBtn.className = "btn btn-outline btn-sm";
        viewBtn.textContent = "View";
        viewBtn.onclick = () => viewHistoryJob(job.job_id, job.total_cases);
        actions.appendChild(viewBtn);

        const dlBtn = document.createElement("button");
        dlBtn.className = "btn btn-outline btn-sm";
        dlBtn.textContent = "CSV";
        dlBtn.onclick = () => { window.location.href = `${API_BASE}/jobs/${job.job_id}/cases/export`; };
        actions.appendChild(dlBtn);
      }

      item.appendChild(meta);
      item.appendChild(actions);
      historyList.appendChild(item);
    });

    historySection.style.display = "block";
  } catch (err) {
    console.error("loadPastSearches error:", err);
  }
}

async function viewHistoryJob(jobId, totalCount) {
  currentJobId = jobId;
  hideError();
  hideProgress();
  await loadCases(jobId, totalCount);
}

// ── UI helpers ───────────────────────────────────────────────────────────
function showProgress(message, pct, done, total) {
  progressSection.style.display = "block";
  progressFill.style.width = pct + "%";
  progressPctLabel.textContent = pct + "%";
  progressYearsLabel.textContent = (done != null && total != null)
    ? `${done} / ${total} years`
    : "";
  progressMsg.textContent = message || "";
}

function hideProgress()  { progressSection.style.display = "none"; }
function hideResults()   { resultsSection.style.display = "none"; }

function setFormDisabled(disabled) {
  submitBtn.disabled = disabled;
  nameInput.disabled = disabled;
  yearInput.disabled = disabled;
  submitBtn.textContent = disabled ? "Searching…" : "Search Cases";
}

function showError(msg) {
  errorText.textContent = msg;
  errorBanner.style.display = "flex";
}

function hideError() { errorBanner.style.display = "none"; }

function resetToSearch() {
  hideResults();
  hideProgress();
  hideError();
  clearPolling();
  currentJobId = null;
  setFormDisabled(false);
  nameInput.focus();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
