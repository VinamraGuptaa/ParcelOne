# icy-disk — Living Architecture Doc

> Last updated: 2026-05-29 (auth: users, server-side sessions, per-user workflows)
> Update this file whenever a decision is made, a component changes, or a known issue is resolved.

---

## What the App Does

**icy-disk** is a full-stack property due-diligence tool for Maharashtra. It chains:

1. **Bhulekh** — 7/12 land record scrape (district / taluka / village / survey)
2. **IGR** — registration history for the survey
3. **eCourts** — litigation search via API (with scraper fallback)

**Primary user flow (land workflow):**

1. User selects location + survey in the React UI (`/search`)
2. Backend creates a `LandCaseWorkflow`, returns `workflow_id` immediately (202)
3. Background worker runs Bhulekh → name variants → IGR → eCourts ranking
4. Frontend polls `/api/workflows/{id}` every 3 seconds
5. On `ranked_done`: report page shows IGR timeline, litigation signals, ranked hits
6. Past workflows appear on the dashboard and sidebar

**Secondary flow (eCourts name search):**

1. User enters petitioner name (+ optional year) on `/search` (Name tab)
2. Classic Playwright/Hybrid scraper job via `/api/jobs`
3. Results at `/report/job/{id}` with CSV export

**CLI:** `uv run python main.py "Name" --year 2017 --headless`

---

## Current Architecture

```
Browser (React SPA — frontend/dist)
    │  same-origin /api  (VITE_API_BASE=/api in Docker build)
    ▼
FastAPI  (uvicorn, server.py, port 8000)
    ├── GET  /api/health              → liveness
    ├── GET  /api/health/db           → schema check (SQLite PRAGMA)
    ├── POST /api/workflows/land-case-search  → start land workflow (202)
    ├── GET  /api/workflows           → list workflows
    ├── GET  /api/workflows/{id}      → poll status + progress_pct
    ├── GET  /api/workflows/{id}/results
    ├── GET  /api/workflows/{id}/artifacts
    ├── GET  /api/workflows/{id}/artifact/{pdf|csv|html}
    ├── POST /api/jobs                → eCourts name search job (202)
    ├── GET  /api/jobs / {id} / cases / export
    ├── POST /api/auth/register     → sign up + session cookie
    ├── POST /api/auth/login        → login + session cookie
    ├── POST /api/auth/logout       → revoke session (DELETE row)
    ├── GET  /api/auth/me           → current user
    ├── GET  /api/auth/config       → { auth_enabled, allow_register }
    └── SPA catch-all                 → index.html for React Router deep links

Background workers
    ├── api/land_case_worker.py   → run_land_case_workflow()
    └── api/worker.py             → run_scrape_job()

Scrapers
    ├── bhulekh_scraper.py        → 7/12 via Playwright
    ├── igr_freesearch_scraper.py → IGR registration records
    ├── scraper.py                → HybridECourtsScraper (browser bootstrap + HTTP)
    └── ecourts_api_client.py     → official eCourts API (when ECOURTS_API_KEY set)

Database (SQLAlchemy async)
    ├── Dev:  SQLite  (sqlite+aiosqlite:///./ecourts.db)  — WAL mode
    └── Prod: PostgreSQL on AWS RDS (postgresql+asyncpg://...)
```

### Deployment (AWS)

Single Docker image serves **both** API and React UI. Typical stack:

| Layer | AWS service | Notes |
|---|---|---|
| Compute | **ECS Fargate** (or EKS) | Runs `Dockerfile` image; `PORT=8000` |
| Load balancer | **ALB** | Health check: `GET /api/health` |
| Database | **RDS PostgreSQL** | `DATABASE_URL` env var on task |
| Registry | **ECR** | `docker build` → push → deploy |
| Secrets | **Secrets Manager** / SSM | `ECOURTS_API_KEY`, DB credentials |
| Optional CDN | **CloudFront** in front of ALB | TLS + caching for static assets |

**Not used:** Render, Netlify (removed — redundant for AWS same-origin deploy).

**Docker build:**

```bash
docker build -t icy-disk .
docker run -p 8000:8000 -e DATABASE_URL=... icy-disk
```

Stage 1 (`node:22-slim`): `npm ci && npm run build` with `VITE_API_BASE=/api`.  
Stage 2 (`python:3.11-slim`): backend + Playwright + copy `frontend/dist`.

**Local dev:**

```bash
# Terminal 1 — API
uv run python server.py

# Terminal 2 — Vite dev server (proxies /api → :8000)
cd frontend && npm run dev
```

---

## Authentication and sessions

Optional gate for pilot users. **On by default in production** (`DEV=0` in Docker); off locally when `DEV=1`. Override with `AUTH_ENABLED=0` or `AUTH_ENABLED=1`.

### Data model

| Table | Purpose |
|-------|---------|
| `users` | `email` (unique), `password_hash` (bcrypt), `is_admin` |
| `sessions` | Server-side sessions: `token_hash` (SHA-256, **unique index**), `user_id`, `expires_at` |
| `land_case_workflows.user_id` | Workflow ownership |
| `search_jobs.user_id` | eCourts job ownership |

Pre-auth rows with `user_id=NULL` are invisible once auth is enabled.

### Session lifecycle

1. **Register / login** → `secrets.token_urlsafe(32)` raw token → HttpOnly cookie `session_token` **and** `session_token` in JSON (stored in `sessionStorage` by the SPA)
2. **DB stores** only `SHA-256(raw_token)` in `sessions.token_hash` (indexed unique lookup)
3. **Each request** → session from cookie **or** `Authorization: Bearer <token>` → reject if missing or expired
4. **Logout** → `DELETE FROM sessions WHERE token_hash = ?` + clear cookie (immediate revocation)
5. **Startup** → purge expired session rows

No JWT, no Starlette signed-cookie sessions — tokens are revocable and indexed in the DB.

### API protection

When `AUTH_ENABLED=1`, all `/api/*` routes except `/api/health`, `/api/health/db`, and `/api/auth/*` require a valid session.

Workflow and job list/get/create queries filter by `sessions.user_id → users.id`.

The active-workflow concurrency guard is **per user** (User A running does not block User B).

### Frontend

| Route | Page |
|-------|------|
| `/signup` | First-time registration |
| `/login` | Returning users |

`AuthContext` calls `/api/auth/me` on load; redirects to `/login` when auth is enabled and unauthenticated. All `fetch` calls use `credentials: 'include'`.

### Environment

| Variable | Default | Notes |
|----------|---------|-------|
| `AUTH_ENABLED` | (unset) | Unset + `DEV=0` → on; `DEV=1` → off. Set `0`/`1` to override |
| `AUTH_ALLOW_REGISTER` | `1` | Set `0` to close public sign-up |
| `AUTH_SESSION_MAX_AGE` | `604800` | Session TTL in seconds (7 days) |
| `AUTH_ADMIN_EMAIL` | unset | Bootstrap admin account on startup (with `AUTH_ADMIN_PASSWORD`) |
| `AUTH_ADMIN_PASSWORD` | unset | Admin password (min 8 chars); synced on startup if changed in env |

On startup, if `AUTH_ADMIN_EMAIL` and `AUTH_ADMIN_PASSWORD` are set, the server creates that user as admin (or promotes/updates an existing account).

---

## Key Decisions

### 1. Same-origin deploy on AWS
**Why:** One container, one ALB target group. React calls `/api` with no CORS complexity. Docker build bakes `VITE_API_BASE=/api`.

### 2. SPA catch-all in FastAPI
**Why:** React Router paths (`/report/workflow/:id`) must return `index.html`. ALB alone does not rewrite paths; `api/app.py` serves static assets under `/assets` and `/data`, then falls back to `index.html`.

### 3. Playwright over Selenium
**Why:** Async-native; shares asyncio event loop with FastAPI.

### 4. CAPTCHA via RapidOCR (ONNX)
**Why:** ~130MB RAM vs PyTorch/EasyOCR ~400MB. Models pre-downloaded in Docker build.

### 5. HybridECourtsScraper
**Why:** Browser once for PHPSESSID, then HTTP for all year searches (~35% faster).

### 6. `asyncio.create_task()` for background jobs
**Why:** No queue infrastructure. Trade-off: tasks lost on restart (orphan recovery on startup).

### 7. Inline `ALTER TABLE` migrations on startup
**Why:** No Alembic yet. `create_all()` + guarded ALTERs in `api/app.py` for existing RDS/SQLite DBs.

### 8. IGR structured fields in `raw_json`
**Why:** Avoids schema migrations for IGR column drift; parsed at read time in `api/routes/workflows.py`.

---

## Data Model

### Legacy eCourts jobs
- `search_jobs` — petitioner name search jobs
- `cases` — scraped case rows per job

### Land workflow (primary)
- `land_case_workflows` — orchestration state, location, survey, progress
- `land_entities` — extracted 7/12 occupant data
- `name_variants` — normalized name variants for eCourts search
- `workflow_case_hits` — ranked scraper/API case matches
- `workflow_igr_hits` — IGR registration rows (`raw_json`)
- `ecourts_api_calls` — API audit log per workflow
- `ecourts_api_cases` — ranked eCourts API case rows
- `ecourts_rank_cache` — cross-workflow rank cache

Workflow terminal status: `ranked_done` | `failed`

---

## File Structure

```
icy-disk/
├── Dockerfile              Multi-stage: Node (React) + Python (API)
├── pyproject.toml          uv-managed Python deps; project name icy-disk
├── server.py               PORT from env; starts uvicorn
├── .env.example            DATABASE_URL, CORS, ECOURTS_API_KEY, etc.
│
├── frontend/               React + Vite + TypeScript SPA
│   ├── src/api/client.ts   API types + fetch helpers (API_BASE=/api)
│   ├── src/pages/          Dashboard, Search, Workflow/Job reports
│   └── public/data/        bhulekh_catalog.json
│
├── api/
│   ├── app.py              FastAPI factory; CORS; SPA serving; DB migrations
│   ├── models.py           SQLAlchemy ORM
│   ├── schemas.py          Pydantic request/response
│   ├── land_case_worker.py Land workflow orchestration
│   ├── worker.py           eCourts scrape jobs
│   └── routes/             jobs, cases, workflows
│
├── static/                 Legacy vanilla JS UI (fallback if no frontend/dist)
│
├── scraper.py              HybridECourtsScraper
├── bhulekh_scraper.py
├── igr_freesearch_scraper.py
│
└── tests/
    ├── test_auth.py                     Auth API (register, login, sessions, isolation)
    ├── test_auth_edge_cases.py          Refresh/bearer/sliding session/public routes
    ├── test_auth_helpers.py             Auth env/cookie/bearer unit tests
    ├── test_auth_frontend_contract.py   Frontend session storage & 401 handling
    ├── test_frontend_react_contract.py  Frontend ↔ backend API contract
    ├── test_frontend_spa_integration.py   SPA + API same-origin
    └── test_workflow_api.py
```

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLite (dev) or RDS PostgreSQL (prod) |
| `CORS_ORIGINS` | Comma-separated origins; `*` ok for same-origin AWS |
| `PORT` | HTTP port (default 8000) |
| `ECOURTS_API_KEY` | eCourts API key (`eci_…`) |
| `ECOURTS_ALLOW_SCRAPER_FALLBACK` | Fall back to Playwright scraper if API unavailable |
| `PLAYWRIGHT_BROWSERS_PATH` | Chromium cache path |
| `MAX_JOBS` | Retention limit for completed eCourts name-search jobs |
| `DEV` | `1` = uvicorn reload (local only) |

Frontend build (Docker only): `VITE_API_BASE=/api`

---

## Test Coverage

Run: `uv run pytest -v`

Auth tests cover: registration/login, bearer token without cookies (page-refresh simulation), sliding session expiry, logout revocation, expired/invalid tokens, per-user workflow/job isolation, admin bootstrap, public vs protected routes, and frontend contracts (`localStorage` token, `ApiError` status 401 handling, cached user on reload).

Contract tests ensure React source stays aligned with FastAPI schemas and routes. SPA integration tests require `cd frontend && npm run build` first.

---

## Known Issues

### Active
- **`/api/health/db` is SQLite-only** — uses `PRAGMA table_info`; needs `information_schema` for RDS.
- **Single concurrency** — one active land workflow at a time (409 guard); scrapers share one event loop.
- **Legacy `static/`** — kept as dev fallback; production uses React from Docker build.

### Resolved
- **Render/Netlify split deploy** — removed; AWS same-origin Docker replaces it.
- **SPA deep links** — FastAPI catch-all serves `index.html` for React Router paths.
- **Orphaned jobs on restart** — `_recover_orphaned_jobs()` marks stuck jobs/workflows `failed` on startup.

---

## Repo Naming

Everything uses **icy-disk**:

- Python package: `pyproject.toml` → `name = "icy-disk"`
- Frontend package: `frontend/package.json` → `icy-disk-frontend`
- UI brand: `frontend/src/config/brand.ts` → `BRAND_NAME = 'Plotwise'` (browser tab title matches)
- Docker image / ECS service: `icy-disk`

Ensure the git remote points at your canonical **icy-disk** repository (update with `git remote set-url origin …` if migrating from an old name).
