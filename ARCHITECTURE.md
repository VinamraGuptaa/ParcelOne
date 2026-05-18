# eCourts India Case Scraper — Living Architecture Doc

> Last updated: 2026-04-13 (hybrid browser+HTTP scraper; RapidOCR; Render Docker deployment)
> Update this file whenever a decision is made, a component changes, or a known issue is resolved.

---

## What the App Does

A full-stack web tool that scrapes case records from [eCourts India](https://services.ecourts.gov.in) for a given petitioner name and optional filing year. Hardcoded to:
- **State:** Maharashtra
- **District:** Pune
- **Court Complex:** Pune, District and Sessions Court
- **Case Status filter:** Both (Pending + Disposed)

**User flow:**
1. User enters a petitioner name (≥ 3 chars) and optional year in the web UI
2. Backend creates a job record, returns `job_id` immediately (202)
3. Playwright scraper runs in the background: solves CAPTCHA per year, parses the summary table, then fetches a detail page per case
4. Frontend polls every 3 seconds; shows a progress bar (years done / years total)
5. On completion: renders a case table and offers CSV download
6. Past searches are listed with View / CSV buttons

**CLI mode** also works: `uv run python main.py "Name" --year 2017 --headless`

---

## Current Architecture

```
Browser (static HTML/JS)
    │  polls /api/jobs/{id} every 3s
    ▼
FastAPI  (uvicorn, server.py)
    ├── POST /api/jobs          → creates SearchJob, fires asyncio.create_task()
    ├── GET  /api/jobs          → list past jobs (newest first)
    ├── GET  /api/jobs/{id}     → poll status + progress_pct
    ├── GET  /api/jobs/{id}/cases           → paginated Case rows
    ├── GET  /api/jobs/{id}/cases/export    → CSV download (StreamingResponse)
    └── GET  /api/health        → liveness check (also used for Render cold-start UX)

Background worker (api/worker.py)
    └── run_scrape_job(job_id)
        ├── Opens its own AsyncSession (not the request session)
        ├── Calls HybridECourtsScraper (search_petitioner per year)
        ├── Iterates over years, inserts Case rows per year
        └── Updates job.status / progress_message / years_done after each year

HybridECourtsScraper (scraper.py)  ← primary scraper
    ├── setup_driver()          → no-op (browser deferred to bootstrap)
    ├── navigate_and_select()   → no-op
    ├── bootstrap_session()     → launches Chromium ONCE, loads index page,
    │                             extracts PHPSESSID cookie, closes browser (~10s)
    ├── search_petitioner()     → bootstraps on first call (or stale session),
    │                             then delegates to _http_search_year()
    │       ├── _fetch_captcha_http()   → GET securimage PNG with session cookie,
    │       │                             solve via RapidOCR (~0.5s, no browser)
    │       ├── _http_search_year()    → POST submitPartyName, parse summary HTML
    │       │       └── _parse_summary_table(html=...)  → rows + onclick strings
    │       └── _http_fetch_detail()   → POST viewHistory, parse detail HTML
    │               └── _parse_detail_page(html=...)    → BeautifulSoup fields
    └── close()                 → closes browser if still open (usually already closed)

ECourtsScraper (scraper.py)  ← base class / browser-only fallback
    ├── setup_driver()          → launches Playwright Chromium (headless)
    ├── navigate_and_select()   → fills State/District/Court dropdowns (AJAX)
    ├── search_petitioner()     → solves CAPTCHA via browser, submits, parses
    │       ├── _parse_summary_table(html=None)  → browser page or passed HTML
    │       └── _fetch_detail_by_onclick()        → calls viewHistory() via JS eval
    │               └── _parse_detail_page(html=None)  → browser page or passed HTML
    └── close()                 → browser.close() + playwright.stop()

Database (SQLAlchemy async)
    ├── Dev:  SQLite  (sqlite+aiosqlite:///./ecourts.db)  — WAL mode enabled
    └── Prod: PostgreSQL on Render (postgresql+asyncpg://...)

Static frontend
    ├── static/index.html
    ├── static/app.js       — vanilla JS, no build step
    └── static/style.css
```

### Deployment Targets (free tier)
| Layer | Service | Notes |
|---|---|---|
| Backend | Render (Docker) | Spins down after 15 min idle; ~30-60s cold start |
| Frontend | Netlify | `static/` published directly via `netlify.toml` |
| Database | Render PostgreSQL | Free tier; `DATABASE_URL` env var (auto-populated via Blueprint) |

`API_BASE` in `app.js` must be updated to the Render URL for a deployed frontend. Set to `/api` for same-origin (local dev / Render serving static directly).

---

## Key Decisions

### 1. Playwright over Selenium
**Why:** Playwright is async-native, eliminating the need for `ThreadPoolExecutor`. FastAPI and the scraper both run on the same asyncio event loop. Also has better `wait_for_selector` ergonomics vs Selenium's `WebDriverWait`.

### 2. CAPTCHA via RapidOCR (local inference)
**Why:** eCourts uses a simple alphanumeric image CAPTCHA (Securimage). Originally used EasyOCR but it pulls PyTorch (~400MB RAM, 35s init), which OOM-killed Chromium on Render's 512MB free tier. Replaced with RapidOCR (ONNX Runtime) — ~130MB RAM, 0.09s init, same ~80-90% accuracy. The `_get_engine()` singleton avoids reloading the model on every request. Up to 5 retries per year with a fresh captcha image each time. ONNX models are pre-downloaded during Docker `build` to avoid a ~60s download on first request.

### 3. Never call `go_back()` — AJAX navigation only
**Why:** The eCourts detail view (`viewHistory()`) is purely AJAX — the URL never changes from `?p=casestatus/index&app_token=`. Calling `go_back()` after viewing a case detail navigates the browser back to the pre-search form page, making `viewHistory` undefined for all subsequent cases. Fix: call `page.evaluate(view_js)` directly with the extracted `onclick` string; the DOM updates in-place, all 8 cases in a year are fetched correctly.

### 4. Extract `onclick` from summary table, not index-based navigation
**Why:** The original approach iterated by index (`_fetch_detail_by_index(i)`), calling `scroll_into_view` then `click()`. After AJAX re-renders the DOM, stale element references caused timeouts. Extracting the raw `onclick` attribute (e.g. `viewHistory('12345','...')`) and evaluating it directly is stable across re-renders.

### 5. `asyncio.create_task()` for background jobs (no task queue)
**Why:** Zero infrastructure overhead. Acceptable for a single-user / low-concurrency tool. Trade-off: tasks are lost if the server restarts mid-job (job stays `running` forever in DB). A future improvement would be `status=interrupted` on startup for orphaned jobs.

### 6. Worker opens its own `AsyncSessionLocal` sessions
**Why:** The background task outlives the HTTP request session. Sharing the request's `AsyncSession` across tasks causes `DetachedInstanceError`. Each DB update in the worker opens a fresh session, commits, and closes it.

### 7. SQLite WAL mode for local dev
**Why:** Default SQLite journal mode locks the file for the entire write, blocking concurrent reads from the polling endpoint. WAL mode allows concurrent reads while a write is in progress.

### 8. 15-year default range
**Why:** Standardized in both `scraper._get_available_years()` and `worker._last_15_years()`. Covers 2012–2026 (current year back 14). The eCourts site only has reliable data back ~15 years for most court complexes.

### 9. `raw_json` column on Case
**Why:** Stores the full scraped dict as JSON. Provides forward compatibility — if new fields are added to the scraper, they're preserved even before the schema is updated.

### 10. Mock patch target for tests: `scraper.HybridECourtsScraper`
**Why:** `run_scrape_job()` uses a local import (`from scraper import HybridECourtsScraper`) to avoid circular imports at module load time. This means `unittest.mock.patch` must target `scraper.HybridECourtsScraper`, not `api.worker.HybridECourtsScraper`.

### 11. HybridECourtsScraper: browser once, HTTP for all year searches
**Why:** The original scraper opened a Playwright browser for every year (15 full navigations × ~57s = ~14 min). Each navigation required: page load → 3 AJAX dropdown selections → captcha solve → form submit. The hybrid approach uses the browser only to establish a PHP session (PHPSESSID cookie, ~10s), then closes it immediately. All 15 year searches become direct `httpx` POST requests — captcha is refreshed via HTTP GET to Securimage, solved with RapidOCR, then submitted. Expected gain: ~35% faster end-to-end. Chromium runs only ~10-15s instead of 14+ minutes.

### 12. Captcha-per-request via HTTP (no browser needed after bootstrap)
**Why:** Securimage stores the captcha value in the PHP session. Once the PHPSESSID is established (by the bootstrap browser load), `_fetch_captcha_http()` can GET the captcha image URL directly with the session cookie — the server generates a new captcha and stores it in the session. The client then solves it and sends it in the next POST. This avoids any browser automation for captcha refresh.

### 13. Per-parser `html=None` param for browser/HTTP flexibility
**Why:** `_parse_summary_table` and `_parse_detail_page` accept an optional `html` string. When `None` (browser path), they call `await self.page.content()`. When provided (HTTP path), they skip the browser entirely. This design keeps both paths sharing the same parsing logic with zero duplication, and makes the methods easily unit-testable without a real browser.

### 14. Render Docker (not native Python runtime)
**Why:** Render's native Python runtime can't run `apt-get`, so Playwright's Chromium system library dependencies (libnss3, libatk, etc.) can't be installed. The Docker runtime gives full control over the image. The Dockerfile installs all ~15 required system libraries, then `playwright install chromium`, then pre-downloads RapidOCR ONNX models to avoid a ~60s runtime download that would fail Render's health check. `opencv-python-headless` is force-installed over `opencv-python` to avoid `libGL.so.1` segfault in headless containers.

---

## Data Model

### `search_jobs`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID (str) PK | Generated with `uuid4()` |
| `petitioner_name` | Text | Min 3 chars, validated by Pydantic |
| `year` | Text nullable | NULL = scrape last 15 years |
| `status` | String(20) | `pending / running / done / failed` |
| `progress_message` | Text nullable | Human-readable current step |
| `years_total` | Integer nullable | Set once years list is determined |
| `years_done` | Integer | Incremented after each year |
| `total_cases` | Integer | Running count across all years |
| `error_message` | Text nullable | Set on exception |
| `created_at / started_at / finished_at` | DateTime(tz) | UTC |

### `cases`
Summary fields: `search_year`, `sr_no`, `case_type_number_year`, `petitioner_vs_respondent`

Detail fields (15 total): `cnr_number`, `case_type`, `filing_number`, `filing_date`, `registration_number`, `registration_date`, `efiling_number`, `efiling_date`, `under_acts`, `first_hearing_date`, `next_hearing_date`, `case_stage`, `decision_date`, `case_status`, `nature_of_disposal`, `court_number_judge`, `petitioner_and_advocate`, `respondent_and_advocate`

Also: `raw_json` (full dict), `created_at`.

---

## Scraped Fields Per Case

From the summary table:
- Sr No, Case Type/Number/Year, Petitioner vs Respondent

From the detail page (via `viewHistory()` AJAX):
| Field | Populated when |
|---|---|
| CNR Number | Always |
| Case Type | Always |
| Filing Number / Date | Always |
| Registration Number / Date | Always |
| eFiling Number / Date | Only if e-filed |
| Under Acts | Criminal cases |
| First Hearing Date | Always |
| **Next Hearing Date** | Pending cases only |
| **Case Stage** | Pending cases only (e.g. `Notice_Unready`, `Awaiting R and P`) |
| Decision Date | Disposed cases only |
| Case Status | Always (`Case disposed` / null for pending) |
| Nature of Disposal | Disposed cases only |
| Court Number / Judge | Always |
| Petitioner and Advocate | Always |
| Respondent and Advocate | Always |

---

## Performance

### HybridECourtsScraper (current)
Expected timing for "Rajesh Gupta" (59 records, 15 years):
- **Bootstrap (browser):** ~10-15s (one page load to get PHPSESSID)
- **Per-year (HTTP):** ~2.5s overhead + ~10s/record (detail POST + rate-limit delay)
- **Total estimate:** ~15s bootstrap + 15 years × 2.5s + 59 records × 10s ≈ **~625s (~10 min)**

HTTP time breakdown per year:
- Captcha GET + RapidOCR solve: ~0.5s
- POST submitPartyName: ~2s
- Per-detail POST (viewHistory): ~2s + ~5-8s rate-limit delay ≈ **~10s/record**
- Years with 0 results: ~3s (just captcha + POST)

### ECourtsScraper (browser-only, baseline)
Observed timing for "Rajesh Gupta" (59 records, 15 years, headless):
- **Total time:** ~864 seconds (14 min 24 sec)
- **Per-year average:** ~57.6 sec (navigation ~8s + CAPTCHA ~5s + delays + 8s/record)
- **Years with 0 results:** ~25 sec each (navigation + CAPTCHA overhead)

---

## Test Coverage

212 tests across 8 files, run with `uv run pytest`. In-memory SQLite (`StaticPool`) for all API/worker tests.

| File | Count | What it covers |
|---|---|---|
| `test_schemas.py` | ~22 | Pydantic validation, `progress_pct` computed field |
| `test_database.py` | ~8 | URL normalization (SQLite default, postgresql:// → asyncpg) |
| `test_api_jobs.py` | ~26 | Health, POST/GET /api/jobs, pagination, ordering |
| `test_api_cases.py` | ~17 | List cases, pagination, job isolation, CSV export |
| `test_scraper_parsing.py` | ~42 | Summary table parsing, detail page all 15 fields, CSV export, available years |
| `test_captcha_solver.py` | ~12 | Image preprocess, token join, temp cleanup, RapidOCR singleton cache |
| `test_worker.py` | ~12 | Job lifecycle (not-found, running, done, failed), case insertion, `raw_json`, multi-year |
| `test_hybrid_scraper.py` | ~73 | `ScrapingSession`, bootstrap (PHPSESSID extract, browser teardown, error paths), session TTL, HTTP captcha solve, `_http_search_year` (success, captcha retry, session expiry, exhaustion), `_http_fetch_detail` (viewHistory arg mapping, invalid JS), `search_petitioner` (lazy bootstrap, stale re-bootstrap, expiry recovery), no-op overrides, `close()` safety, parser `html=` bypass |

Run: `uv run pytest -v`

---

## File Structure

```
icy-disk/
├── scraper.py              ECourtsScraper (browser) + HybridECourtsScraper (HTTP)
├── captcha_solver.py       RapidOCR singleton; preprocess + solve
├── main.py                 CLI entry point (wraps async with asyncio.run())
├── server.py               Reads $PORT; starts uvicorn
├── Dockerfile              python:3.11-slim + Playwright Chromium system deps
├── netlify.toml            publish = "static" — no build step
├── pyproject.toml          uv-managed deps; pytest config
├── .env.example            DATABASE_URL, CORS_ORIGINS
│
├── api/
│   ├── app.py              FastAPI factory; CORS; static mount; startup hook
│   ├── database.py         Async engine; SQLite WAL; postgresql:// URL fix
│   ├── models.py           SearchJob + Case ORM (SQLAlchemy 2.x Mapped)
│   ├── schemas.py          Pydantic v2 request/response; progress_pct computed
│   ├── worker.py           run_scrape_job(); owns full job lifecycle
│   └── routes/
│       ├── jobs.py         POST/GET /api/jobs, GET /api/jobs/{id}
│       └── cases.py        GET .../cases (paginated), GET .../cases/export (CSV)
│
├── static/
│   ├── index.html
│   ├── app.js              Vanilla JS; API_BASE constant; polling state machine
│   └── style.css
│
└── tests/
    ├── conftest.py         StaticPool SQLite fixture; get_db override; make_job/make_case helpers
    ├── test_schemas.py
    ├── test_database.py
    ├── test_api_jobs.py
    ├── test_api_cases.py
    ├── test_scraper_parsing.py
    ├── test_captcha_solver.py
    ├── test_worker.py
    └── test_hybrid_scraper.py
```

---

## Known Issues

### Active
- **Orphaned jobs on server restart:** ~~Fixed — see Resolved section.~~
- **Scrape timeout:** ~~Fixed — see Resolved section.~~
- **Single concurrency:** `asyncio.create_task` runs jobs on the same event loop. Two simultaneous scrapes will interleave — the HTTP client is per-call so they don't share state, but Render's free tier (0.1 CPU, 512MB RAM) still can't handle two concurrent jobs. The UI guards against this via the concurrent scrape dialog.
- **`main.py` still says "Last 10 years":** The CLI summary printout hardcodes `"Last 10 years"` in the display string, but actually scrapes 15 years. Cosmetic only.
- **Render cold start UX:** The "Waking up server..." banner shows but there's no timeout — it pings forever if Render is truly down, not just sleeping.
- **Hybrid: PHP session TTL unknown for eCourts:** `SESSION_TTL = 1200` (20 min) is conservative. If eCourts sets a shorter `gc_maxlifetime`, the session will expire mid-run. The `SessionExpiredError` handler re-bootstraps automatically, so this is self-healing at the cost of one extra browser open.

### Resolved (recent)
- **Chromium OOM on Render 512MB tier:** EasyOCR imported PyTorch (~400MB RAM) before Chromium could launch, OOM-killing the browser. Fixed by replacing EasyOCR with RapidOCR (ONNX Runtime, ~130MB). PyTorch is no longer a dependency.
- **`libGL.so.1` crash on Docker:** `opencv-python` (full build) tries to init OpenGL in headless containers and segfaults. Fixed by force-installing `opencv-python-headless` in the Dockerfile after `uv sync`.
- **RapidOCR 60s model download at runtime:** First captcha solve triggered a ~15MB ONNX model download, causing a ~60s delay that failed Render's health check. Fixed by adding `RapidOCR()` init in the Dockerfile build step (models cached in image).
- **Chromium launch hanging silently in Docker:** Missing `--no-zygote` and `--single-process` flags for containerized Chromium. Added to `setup_driver()` launch args.
- **14 unnecessary browser re-navigations:** Each year search re-opened a full browser, ran 3 AJAX dropdown selections, solved a captcha, and waited. `HybridECourtsScraper` replaced this with a single bootstrap page load + `httpx` POST per year.

### Resolved
- **Scrape timeout / auto-kill (per-year, record-calibrated):** Each year gets its own `asyncio.timeout` budget calculated from the running average of records seen in prior years: `max(120s, 60s_overhead + estimated_records × 1.5 × 15s)`. First year defaults to 10 records estimate; calibrates itself from year 2 onward. Examples: est 0 records→120s, est 10→285s, est 20→510s, est 50→1185s. On expiry, that year's `TimeoutError` is caught inline, job is marked `failed` with exact budget and estimate in the error message, and `scraper.close()` still runs via `finally`. Hard override via `SCRAPE_TIMEOUT_SECONDS` env var. `api/worker.py:_calc_year_timeout`.
- **Orphaned jobs on server restart:** Jobs stuck in `status=running` after a Render restart now self-heal. `_recover_orphaned_jobs()` runs in the FastAPI `startup` event — it finds all `running` jobs, marks them `failed` with message "Server restarted while scrape was in progress.", and commits. The frontend re-attach logic then sees `failed`, stops polling, shows the error banner, and re-enables the form. `api/app.py:_recover_orphaned_jobs`.
- **Re-attach to running job on page refresh:** Previously, refreshing mid-scrape lost the progress bar and left the user blocked (concurrent guard prevented new searches, but no progress was shown). Now `reattachRunningJob()` runs on `DOMContentLoaded` — it calls `getRunningJob()`, and if a job is running, restores `currentJobId`, re-shows the progress bar with current values, disables the form, and restarts polling. `app.js:reattachRunningJob`.
- **DB retention / storage cleanup:** All jobs and cases were kept forever. `_cleanup_old_jobs()` now runs after every completed job and deletes all but the `MAX_JOBS` (default 10) most recent jobs. Case rows are removed via SQLAlchemy cascade. Configurable via `MAX_JOBS` env var. `api/worker.py:_cleanup_old_jobs`.
- **Concurrent scrape guard dialog:** Submitting a new search while a job is `running` (including after a page refresh mid-scrape) now shows a modal dialog with the active petitioner name and current progress message. Implemented via `getRunningJob()` (polls `GET /api/jobs?limit=20` on submit), `showBusyDialog()` / `hideBusyDialog()`, and a CSS modal overlay. Clicking the overlay or the "OK, I'll Wait" button dismisses it. Files changed: `index.html`, `style.css`, `app.js`.
- **View/CSV buttons shown for zero-case jobs:** Both buttons appeared in history for any `done` job regardless of `total_cases`. Clicking CSV caused a browser navigation to a JSON 404 error page (backend returns 404 when no cases exist). Fixed: buttons now only render when `job.total_cases > 0` (`app.js:277`).
- **`downloadCSV()` not guarded against empty results:** Main results area CSV button would navigate to 404 if somehow invoked on a zero-case job. Added guard in `downloadCSV()` checking the results title text.
- **History label "Last 10 years" instead of "Last 15 years":** `app.js:271` hardcoded the wrong default label. Fixed to "Last 15 years".

- **DB schema out of sync after adding new scraper fields:** `create_all()` on startup only creates missing tables — it never runs `ALTER TABLE` on existing ones. When `next_hearing_date` and `case_stage` were added to `models.py` and `scraper.py`, the live `ecourts.db` SQLite file still had the old schema. Fix: run `ALTER TABLE cases ADD COLUMN <col> TEXT` manually (or via a migration script). **Pattern to follow whenever a new column is added to `Case`: also run the corresponding ALTER TABLE on any existing DB.** For Supabase (prod), use the Supabase SQL editor.
- **Court complex dropdown "did not find some options":** `select_option(label=...)` does an exact string match — fails when the site serves the option label with extra whitespace or minor text drift. Replaced with two new helpers: `_wait_for_option_containing()` (waits until the option is present using partial text match) and `_select_option_containing()` (selects via `el.value = opt.value; el.dispatchEvent(new Event('change'))` in JS). Both use case-insensitive `includes()`. On failure, logs all available option texts for diagnosis. `scraper.py:_wait_for_option_containing`, `_select_option_containing`.
- **Progress bar "x / 10 years" label:** History badge hardcoded "Last 10 years" instead of "Last 15 years". Already fixed with the View/CSV fix (`app.js:271`). Worker and scraper both correctly produce 15 years; `years_total` is set to 15 in the DB.

### Earlier resolved
- **`col_0/col_1/col_2` instead of named columns:** Section header rows (`<tr><th colspan="3">CourtName</th></tr>`) inside `<tbody>` were overwriting the headers list. Fixed by parsing column headers only from `<thead>` and skipping any `<tbody>` row containing `<th>` elements.
- **Only 1/N detail pages fetched:** `go_back()` after the first detail page navigated away from the AJAX context, making `viewHistory` undefined. Fixed by calling `page.evaluate(view_js)` with the extracted `onclick` string directly — no navigation required.
- **Worker test `AttributeError: api.worker has no attribute ECourtsScraper`:** Local import inside `run_scrape_job()` requires patching `scraper.ECourtsScraper`, not `api.worker.ECourtsScraper`.
- **Under Acts test failures:** `find_next_siblings("tr")` cannot cross `<thead>` → `<tbody>` boundaries. Fixed test fixtures to use flat tables for that section.
