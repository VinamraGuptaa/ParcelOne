"""Browser-level frontend tests (Playwright).

These tests validate real DOM interaction for `static/index.html` +
`static/app.js`, while mocking network responses for:
- `/data/bhulekh_catalog.json`
- `/api/workflows/*`
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"


CATALOG_FIXTURE = {
    "generated_at": "2026-04-30T12:00:00Z",
    "districts": [
        {
            "value": "25",
            "label": "पुणे",
            "english": "Pune",
            "talukas": [
                {
                    "value": "7",
                    "label": "हवेली",
                    "english": "Haveli",
                    "villages": [
                        {
                            "value": "v1",
                            "label": "वाघोली",
                            "english": "Wagholi",
                        },
                        {
                            "value": "v2",
                            "label": "म .कर्वेनगर",
                            "english": "Karve Nagar",
                        },
                    ],
                }
            ],
        }
    ],
}


@pytest.fixture(scope="module")
def static_server_base_url():
    """Serve `static/` over HTTP so browser fetch('/...') works."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(STATIC_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest_asyncio.fixture
async def page() -> Page:
    """Provide a Playwright page, skip test when browser unavailable."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-vulkan",
                    "--use-angle=swiftshader",
                    "--use-gl=swiftshader",
                ],
            )
        except PlaywrightError as exc:  # pragma: no cover - env dependent
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        context = await browser.new_context()
        pg = await context.new_page()
        # Speed up poll loop (`setInterval`) for tests.
        await pg.add_init_script(
            """
            (() => {
              const orig = window.setInterval;
              window.setInterval = (fn, ms, ...args) => orig(fn, Math.min(ms, 25), ...args);
            })();
            """
        )
        try:
            yield pg
        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
async def test_cascading_dropdowns_from_catalog(page: Page, static_server_base_url: str):
    async def route_handler(route):
        url = route.request.url
        if url.endswith("/data/bhulekh_catalog.json"):
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(CATALOG_FIXTURE, ensure_ascii=False),
            )
            return
        await route.continue_()

    await page.route("**/*", route_handler)
    await page.goto(f"{static_server_base_url}/index.html")

    # Catalog loaded -> district select populated.
    await page.wait_for_selector("#district option[value='0']", state="attached")
    district_text = await page.locator("#district option[value='0']").inner_text()
    assert "Pune" in district_text

    await page.select_option("#district", "0")
    await page.wait_for_selector("#taluka option[value='0']", state="attached")
    taluka_text = await page.locator("#taluka option[value='0']").inner_text()
    assert "Haveli" in taluka_text

    await page.select_option("#taluka", "0")
    village_enabled = await page.locator("#village").is_enabled()
    assert village_enabled is True

    options = await page.locator("#village option").evaluate_all(
        "nodes => nodes.map(n => n.textContent || '')"
    )
    assert any("Wagholi" in opt for opt in options)
    assert any("Karve Nagar" in opt for opt in options)


@pytest.mark.asyncio
async def test_submit_poll_and_render_results(page: Page, static_server_base_url: str):
    poll_count = {"n": 0}

    async def route_handler(route):
        req = route.request
        url = req.url

        if url.endswith("/data/bhulekh_catalog.json"):
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(CATALOG_FIXTURE, ensure_ascii=False),
            )
            return

        if url.endswith("/api/workflows/land-case-search") and req.method == "POST":
            payload = json.loads(req.post_data or "{}")
            # Ensure frontend sends Marathi labels as source-of-truth.
            assert payload["district_label"] == "पुणे"
            assert payload["taluka_label"] == "हवेली"
            assert payload["village_label"] == "वाघोली"
            await route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(
                    {
                        "workflow_id": "wf_123",
                        "status": "pending_input",
                        "progress_message": "Workflow accepted.",
                        "progress_pct": 0,
                    }
                ),
            )
            return

        if url.endswith("/api/workflows/wf_123") and req.method == "GET":
            poll_count["n"] += 1
            if poll_count["n"] < 2:
                body = {
                    "workflow_id": "wf_123",
                    "status": "ecourts_running",
                    "progress_message": "Searching eCourts...",
                    "progress_pct": 45,
                    "years_done": 9,
                    "years_total": 20,
                    "total_hits": 0,
                }
            else:
                body = {
                    "workflow_id": "wf_123",
                    "status": "done",
                    "progress_message": "Ranking complete.",
                    "progress_pct": 100,
                    "years_done": 20,
                    "years_total": 20,
                    "total_hits": 1,
                }
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(body),
            )
            return

        if url.endswith("/api/workflows/wf_123/results") and req.method == "GET":
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "workflow_id": "wf_123",
                        "owner_name": "Snehal Bhooshan Dhoot",
                        "entity": {
                            "occupant_primary_name": "Snehal Bhooshan Dhoot",
                            "occupant_candidates": ["Snehal Bhooshan Dhoot"],
                            "mutation_numbers": ["20133"],
                            "extraction_confidence": 0.95,
                        },
                        "hits": [
                            {
                                "final_rank": 1,
                                "court": "Civil Judge, Pune",
                                "parties_text": "Snehal Bhooshan Dhoot vs State",
                                "case_type": "Regular Civil Appeal",
                                "search_year": "2024",
                                "is_civil": True,
                                "name_match_score": 0.9123,
                                "match_explanation": "owner_exact + pending_civil",
                            }
                        ],
                        "igr_hits": [{"survey_number": "1530/3", "search_year": "2024"}],
                        "total_hits": 1,
                        "ecourts_api_metrics": {"estimated_cost_inr": 1.5},
                        "ecourts_api_calls": [{}],
                        "ecourts_api_cases": [
                            {
                                "cnr_number": "DLND020047882015",
                                "case_type": "CC",
                                "case_type_raw": "Ct Cases",
                                "court": "Chief Metropolitan Magistrate",
                                "court_no": "2",
                                "district": "New Delhi",
                                "state": "DL",
                                "case_number": "202400248072016",
                                "cnr_year": "2015",
                                "filing_number": "27843/2015",
                                "filing_date": "2015-12-21",
                                "registration_number": "24807/2016",
                                "registration_date": "2015-12-21",
                                "first_hearing_date": "2016-01-05",
                                "next_hearing_date": "2018-07-07",
                                "decision_date": "2018-07-07",
                                "petitioners": ["MR. ARUN JAITLEY"],
                                "respondents": ["MR. ARVIND KEJRIWAL"],
                                "petitioner_advocates": [],
                                "respondent_advocates": [],
                                "case_category_facet_path": "Criminal Law/Other Criminal Matters",
                                "parties_text": "MR. ARUN JAITLEY vs MR. ARVIND KEJRIWAL",
                                "case_status": "DISPOSED",
                                "is_civil": False,
                                "is_pending": False,
                                "final_rank": 1,
                                "source_stage": "detail",
                                "raw": {},
                            }
                        ],
                    }
                ),
            )
            return

        if url.endswith("/api/workflows/wf_123/artifacts") and req.method == "GET":
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "workflow_id": "wf_123",
                        "pdf_path": "artifacts/workflows/wf_123_land_record.pdf",
                        "ranked_csv_path": "artifacts/workflows/wf_123_ranked_hits.csv",
                        "html_path": "artifacts/workflows/wf_123_submitted.html",
                    }
                ),
            )
            return

        await route.continue_()

    await page.route("**/*", route_handler)
    await page.goto(f"{static_server_base_url}/index.html")

    # Fill form.
    await page.wait_for_selector("#district option[value='0']", state="attached")
    await page.select_option("#district", "0")
    await page.select_option("#taluka", "0")
    await page.select_option("#village", "वाघोली")
    await page.fill("#survey-part1", "1530")
    await page.fill("#survey-option", "1530/3")
    await page.fill("#owner-name", "Snehal Bhooshan Dhoot")

    await page.click("#submit-btn")

    # Wait for done-state rendering.
    await page.wait_for_selector("#results-section", state="visible")
    title = await page.locator("#results-title").inner_text()
    assert "1 result(s)" in title

    # Hits table row present.
    assert await page.locator("#hits-table tbody tr").count() == 1
    row_text = await page.locator("#hits-table tbody tr").first.inner_text()
    assert "DLND020047882015" in row_text
    assert "Ct Cases" in row_text
    assert "Chief Metropolitan Magistrate" in row_text

    # Artifact links use new stream endpoint.
    hrefs = await page.locator("#artifacts-block a").evaluate_all(
        "nodes => nodes.map(n => n.getAttribute('href'))"
    )
    assert f"/api/workflows/wf_123/artifact/pdf" in hrefs
    assert f"/api/workflows/wf_123/artifact/csv" in hrefs
    assert f"/api/workflows/wf_123/artifact/html" in hrefs
