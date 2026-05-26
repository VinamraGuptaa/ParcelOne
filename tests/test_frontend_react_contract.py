"""Contract tests: React frontend source stays in sync with the FastAPI backend."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from api.schemas import JobCreateRequest, LandCaseWorkflowCreateRequest, WorkflowSummaryResponse


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "frontend" / "src"
FRONTEND_PUBLIC = ROOT / "frontend" / "public"
VITE_CONFIG = ROOT / "frontend" / "vite.config.ts"
GLOBAL_CSS = FRONTEND_SRC / "styles" / "global.css"
BRAND_TS = FRONTEND_SRC / "config" / "brand.ts"
API_CLIENT = FRONTEND_SRC / "api" / "client.ts"

# Paths the React app is allowed to call (relative to API_BASE, default /api).
EXPECTED_FRONTEND_API_PATHS = {
    "/health",
    "/workflows",
    "/workflows/land-case-search",
    "/jobs",
}

# Dynamic path patterns used in fetch strings (regex).
FRONTEND_API_PATH_PATTERNS = [
    r"/workflows/\$\{[^}]+\}",           # /workflows/${id}
    r"/workflows/\$\{[^}]+\}/results",
    r"/workflows/\$\{[^}]+\}/artifacts",
    r"/workflows/\$\{[^}]+\}/artifact/\$\{[^}]+\}",
    r"/jobs/\$\{[^}]+\}",
    r"/jobs/\$\{[^}]+\}/cases",
    r"/jobs/\$\{[^}]+\}/cases/export",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _collect_frontend_tsx_ts() -> str:
    parts = []
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix in (".ts", ".tsx") and path.name != "client.ts":
            parts.append(_read(path))
    return "\n".join(parts)


def test_react_source_tree_exists():
    assert FRONTEND_SRC.is_dir()
    assert (FRONTEND_SRC / "App.tsx").is_file()
    assert (FRONTEND_SRC / "pages" / "WorkflowReportPage.tsx").is_file()


def test_global_css_has_icy_disk_design_tokens():
    css = _read(GLOBAL_CSS)
    for token in ("--newsprint", "--paper", "--ink", "--red", "--f-display", "--f-mono"):
        assert token in css, f"Missing design token {token} in global.css"
    assert ".app-shell" in css
    assert ".metrics-strip" in css
    assert ".data-table" in css


def test_brand_ts_matches_css_color_tokens():
    brand = _read(BRAND_TS)
    css = _read(GLOBAL_CSS)
    assert 'newsprint: "#f0ece2"' in brand or "newsprint: '#f0ece2'" in brand
    assert "--newsprint: #f0ece2" in css


def test_bhulekh_catalog_present_for_frontend():
    catalog = FRONTEND_PUBLIC / "data" / "bhulekh_catalog.json"
    assert catalog.is_file(), "Copy static/data/bhulekh_catalog.json to frontend/public/data/"
    data = json.loads(catalog.read_text(encoding="utf-8"))
    assert "districts" in data
    assert isinstance(data["districts"], list)


def test_vite_proxies_api_for_local_dev():
    cfg = _read(VITE_CONFIG)
    assert "'/api'" in cfg or '"/api"' in cfg
    assert "proxy" in cfg


def test_api_client_default_base_is_same_origin():
    client_src = _read(API_CLIENT)
    assert "VITE_API_BASE" in client_src
    assert "'/api'" in client_src or '"/api"' in client_src


def test_frontend_calls_documented_api_routes_only():
    """Every literal apiGet/apiPost path must map to a real backend route."""
    src = _collect_frontend_tsx_ts() + _read(API_CLIENT)

    static_paths = set(re.findall(r"['\"](/(?:workflows|jobs|health)[^'\"]*)['\"]", src))
    static_paths = {p.split("?")[0] for p in static_paths}

    allowed_literals = EXPECTED_FRONTEND_API_PATHS | {
        "/workflows/land-case-search",
    }
    unknown = sorted(static_paths - allowed_literals)
    assert not unknown, f"Unexpected literal API paths in frontend: {unknown}"


def test_frontend_dynamic_api_paths_match_backend_routes():
    src = _collect_frontend_tsx_ts()
    for pattern in FRONTEND_API_PATH_PATTERNS:
        assert re.search(pattern, src), f"Expected frontend to use API path pattern: {pattern}"


def test_land_workflow_create_payload_fields_match_schema():
    """PropertySearchForm must send fields accepted by LandCaseWorkflowCreateRequest."""
    form_src = _read(FRONTEND_SRC / "pages" / "search" / "PropertySearchForm.tsx")
    schema_fields = set(LandCaseWorkflowCreateRequest.model_fields.keys())
    for field in (
        "district_label",
        "taluka_label",
        "village_label",
        "survey_part1",
        "survey_option_label",
        "owner_name",
    ):
        assert field in form_src, f"PropertySearchForm missing {field}"
        assert field in schema_fields


def test_job_create_payload_fields_match_schema():
    form_src = _read(FRONTEND_SRC / "pages" / "search" / "NameSearchForm.tsx")
    assert "petitioner_name" in form_src
    assert "year" in form_src
    assert "petitioner_name" in JobCreateRequest.model_fields


def test_workflow_summary_fields_match_schema():
    """Dashboard/Sidebar types must match WorkflowSummaryResponse."""
    client_src = _read(API_CLIENT)
    schema_fields = set(WorkflowSummaryResponse.model_fields.keys())
    for field in (
        "workflow_id",
        "status",
        "village_label",
        "survey_option_label",
        "total_hits",
        "created_at",
    ):
        assert field in client_src, f"WorkflowSummary type missing {field}"
        assert field in schema_fields


def test_workflow_report_polls_terminal_statuses():
    report_src = _read(FRONTEND_SRC / "pages" / "WorkflowReportPage.tsx")
    for status in ("ranked_done", "failed"):
        assert status in report_src


def test_property_form_handles_409_conflict():
    form_src = _read(FRONTEND_SRC / "pages" / "search" / "PropertySearchForm.tsx")
    assert "409" in form_src
    assert "already in progress" in form_src.lower() or "409" in form_src
