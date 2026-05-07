"""Contract tests for the static frontend assets.

These tests are intentionally lightweight and avoid a browser runtime.
They lock down:
- required form fields and element ids in ``static/index.html``
- key API route usage and workflow behaviors in ``static/app.js``
"""

from __future__ import annotations

from pathlib import Path
import re

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "static/index.html"
APP_JS = ROOT / "static/app.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_index_has_required_land_workflow_inputs():
    soup = BeautifulSoup(_read(INDEX_HTML), "html.parser")

    form = soup.select_one("form#land-form")
    assert form is not None

    # Required cascading dropdown/text controls.
    assert soup.select_one("select#district[required]") is not None
    assert soup.select_one("select#taluka[required]") is not None
    assert soup.select_one("select#village[required]") is not None
    assert soup.select_one("input#survey-part1[required]") is not None
    assert soup.select_one("input#survey-option[required]") is not None

    # Optional owner input.
    owner = soup.select_one("input#owner-name")
    assert owner is not None
    assert owner.get("required") is None

    # Submit and reset actions.
    assert soup.select_one("button#submit-btn[type='submit']") is not None
    assert soup.select_one("button#reset-btn[type='button']") is not None


def test_index_has_progress_results_and_artifacts_sections():
    soup = BeautifulSoup(_read(INDEX_HTML), "html.parser")

    assert soup.select_one("#progress-section") is not None
    assert soup.select_one("#progress-status-label") is not None
    assert soup.select_one("#progress-pct-label") is not None
    assert soup.select_one("#progress-msg") is not None
    assert soup.select_one("#progress-meta") is not None

    assert soup.select_one("#results-section") is not None
    assert soup.select_one("table#hits-table") is not None
    assert soup.select_one("#counts-strip") is not None
    assert soup.select_one("#entity-block") is not None
    assert soup.select_one("#artifacts-block") is not None


def test_index_references_app_js_and_legacy_link():
    soup = BeautifulSoup(_read(INDEX_HTML), "html.parser")

    script = soup.select_one("script[src='app.js']")
    assert script is not None

    legacy_link = soup.select_one("a[href='_legacy_jobs.html']")
    assert legacy_link is not None


def test_app_js_uses_expected_workflow_routes():
    js = _read(APP_JS)

    # Static catalog + workflow submit/poll/results/artifacts.
    assert 'const CATALOG_URL = "/data/bhulekh_catalog.json";' in js
    assert 'fetch(`${API_BASE}/workflows/land-case-search`' in js
    assert 'fetch(`${API_BASE}/workflows/${workflowId}`)' in js
    assert 'fetch(`${API_BASE}/workflows/${workflowId}/results`)' in js
    assert 'fetch(`${API_BASE}/workflows/${workflowId}/artifacts`)' in js

    # Artifact stream endpoint usage.
    assert "artifact/${kind}" in js


def test_app_js_has_required_behaviors():
    js = _read(APP_JS)

    # Poll cadence and startup wiring.
    assert "const POLL_INTERVAL_MS = 3000;" in js
    assert "const MAX_POLL_CONSECUTIVE_FAILURES = 10;" in js
    assert "pollConsecutiveFailures" in js
    assert 'document.addEventListener("DOMContentLoaded"' in js
    assert 'districtSel.addEventListener("change", onDistrictChange);' in js
    assert 'talukaSel.addEventListener("change", onTalukaChange);' in js

    # Owner name optional -> null fallback.
    assert "owner_name: ownerInput.value.trim() || null" in js

    # Completion/terminal status handling expected from backend worker lifecycle.
    assert "function isTerminalSuccessStatus(wf)" in js
    assert 'status === "completed"' in js
    assert 'status === "succeeded"' in js
    assert 'status === "done"' in js
    assert 'status === "ranked_done"' in js
    assert "pct >= 100 && msg.includes(\"completed\")" in js
    assert 'wf.status === "failed"' in js
    assert 's === "igr_running"' in js


def test_every_get_element_by_id_reference_exists_in_index_html():
    js = _read(APP_JS)
    soup = BeautifulSoup(_read(INDEX_HTML), "html.parser")

    ids_in_html = {node.get("id") for node in soup.select("[id]")}
    ids_in_js = set(re.findall(r'getElementById\("([^"]+)"\)', js))

    missing = sorted(i for i in ids_in_js if i not in ids_in_html)
    assert not missing, f"IDs referenced by app.js missing in index.html: {missing}"
