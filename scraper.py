"""
eCourts India Case Status Scraper.

Scrapes case data by petitioner name from the eCourts India portal.
Pre-configured for: Maharashtra → Pune → Pune District and Sessions Court.

Rewritten with Playwright (async) — replaces the original Selenium implementation.
Public API is identical; all methods are now async.
"""

import asyncio
import dataclasses
import html as _html
import json as _json
import logging
import os
import random
import re as _re
import time
from typing import Optional

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

# captcha_solver is imported lazily inside solve_captcha() to defer torch
# loading until after Chromium is already running (saves ~400MB at launch time)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_URL = (
    "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/index&app_token="
)

STATE_TEXT = "Maharashtra"
DISTRICT_TEXT = "Pune"
COURT_COMPLEX_TEXT = "Pune, District and Sessions Court"

MIN_DELAY_SECONDS = 3
MAX_DELAY_SECONDS = 7
MAX_CAPTCHA_RETRIES = 5
DEBUG_ARTIFACTS = os.getenv("SCRAPER_DEBUG_ARTIFACTS", "") == "1"
HTTP_TIMEOUT_SECONDS = 45.0
CAPTCHA_FETCH_RETRIES = 3

# ── Hybrid HTTP endpoints (confirmed via live site inspection) ────────────────
# Pune District Court — values taken directly from the live page's form fields
_STATE_CODE = "1"
_DIST_CODE = "25"
_COURT_COMPLEX_CODE = "1010303@1,2,3,22,23@N"  # full select value (not just the code)
_COURT_COMPLEX_BARE = "1010303"  # bare code used in some fields

_BASE = "https://services.ecourts.gov.in/ecourtindia_v6"
_SEARCH_URL = f"{_BASE}/?p=casestatus/submitPartyName"
_GET_CAPTCHA_URL = (
    f"{_BASE}/?p=casestatus/getCaptcha"  # must POST before fetching image
)
_VIEW_HISTORY_URL = f"{_BASE}/?p=home/viewHistory"
_CAPTCHA_URL = f"{_BASE}/vendor/securimage/securimage_show.php"

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{_BASE}/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}


class SessionExpiredError(Exception):
    """Raised when the PHP session appears to have expired."""


@dataclasses.dataclass
class ScrapingSession:
    """Lightweight session state shared across HTTP requests."""

    services_sessid: str  # SERVICES_SESSID cookie
    jsession: str = ""  # JSESSION cookie
    app_token: str = ""  # hidden app_token field in the form
    created_at: float = dataclasses.field(default_factory=time.monotonic)


class ECourtsScraper:
    """Async Playwright-based scraper for eCourts India case status portal."""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    # ------------------------------------------------------------------ #
    #  Setup / Teardown
    # ------------------------------------------------------------------ #

    async def setup_driver(self):
        """Launch Playwright Chromium browser and create a page."""
        import time

        browsers_path = os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH", "~/.cache/ms-playwright"
        )
        logger.info(
            f"Launching Playwright Chromium (headless={self.headless}, browsers_path={browsers_path})..."
        )
        t0 = time.monotonic()
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--single-process",
            ],
        )
        logger.info(f"Chromium launched in {time.monotonic() - t0:.1f}s.")
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self.page = await self.context.new_page()

        # Auto-accept any JS alert dialogs
        self.page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        logger.info("Playwright browser ready.")

    async def close(self):
        """Close the browser and stop Playwright."""
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed.")

    # ------------------------------------------------------------------ #
    #  Navigation
    # ------------------------------------------------------------------ #

    async def navigate_and_select(self):
        """
        Navigate to eCourts and select:
          Maharashtra → Pune → Pune District and Sessions Court
        Also selects 'Both' for case status (Pending + Disposed).
        """
        import time

        logger.info("Navigating to eCourts portal...")
        t0 = time.monotonic()
        await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        logger.info(f"Page loaded in {time.monotonic() - t0:.1f}s.")
        await asyncio.sleep(3)

        # Select State: Maharashtra
        logger.info("Selecting State: Maharashtra...")
        await self.page.wait_for_selector("#sess_state_code", timeout=20000)
        await self.page.select_option("#sess_state_code", label=STATE_TEXT)
        logger.info("State selected. Waiting for district dropdown...")

        await asyncio.sleep(2)
        await self._wait_for_dropdown_populated("#sess_dist_code")

        # Select District: Pune
        logger.info("Selecting District: Pune...")
        await self.page.select_option("#sess_dist_code", label=DISTRICT_TEXT)
        logger.info("District selected. Waiting for court complex dropdown...")

        await asyncio.sleep(2)
        await self._wait_for_option_containing(
            "#court_complex_code", COURT_COMPLEX_TEXT
        )

        # Select Court Complex via partial match (resilient to label whitespace/drift)
        logger.info(f"Selecting Court Complex: {COURT_COMPLEX_TEXT}...")
        await self._select_option_containing("#court_complex_code", COURT_COMPLEX_TEXT)

        await asyncio.sleep(2)

        # Select "Both" radio button for case status
        logger.info("Selecting 'Both' for case status...")
        await self.page.wait_for_selector("#radB", timeout=10000)
        await self.page.click("#radB")
        logger.info("'Both' (Pending + Disposed) selected.")

    async def _wait_for_dropdown_populated(self, selector: str, timeout_s: int = 15):
        """Wait until a select element has more than 1 option (AJAX-populated)."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                locator = self.page.locator(selector)
                count = await locator.locator("option").count()
                if count > 1:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        logger.warning(f"Dropdown {selector} may not be fully populated.")

    async def _wait_for_option_containing(
        self, selector: str, text: str, timeout_s: int = 15
    ):
        """Wait until a select element has an option whose label contains `text`."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            found = await self.page.evaluate(
                """([sel, txt]) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    return Array.from(el.options).some(o =>
                        o.text.trim().toLowerCase().includes(txt.toLowerCase())
                    );
                }""",
                [selector, text],
            )
            if found:
                return
            await asyncio.sleep(0.5)
        # Log available options to help diagnose label mismatches
        options = await self.page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                return el ? Array.from(el.options).map(o => o.text.trim()) : [];
            }""",
            selector,
        )
        logger.warning(
            f"Option containing '{text}' not found in {selector}. Available: {options}"
        )

    async def _select_option_containing(self, selector: str, text: str):
        """Select the first option whose label contains `text` via JS (partial match)."""
        result = await self.page.evaluate(
            """([sel, txt]) => {
                const el = document.querySelector(sel);
                if (!el) return 'no_element';
                const opt = Array.from(el.options).find(o =>
                    o.text.trim().toLowerCase().includes(txt.toLowerCase())
                );
                if (!opt) return 'not_found';
                el.value = opt.value;
                el.dispatchEvent(new Event('change'));
                return opt.text.trim();
            }""",
            [selector, text],
        )
        if result in ("no_element", "not_found"):
            raise Exception(
                f"Could not find option containing '{text}' in {selector}. "
                f"Result: {result}"
            )
        logger.info(f"Selected '{result}' in {selector}.")

    # ------------------------------------------------------------------ #
    #  Captcha
    # ------------------------------------------------------------------ #

    async def solve_captcha(self) -> Optional[str]:
        """Screenshot the captcha element and solve it with EasyOCR."""
        import captcha_solver  # deferred: importing easyocr/torch here keeps RAM free at launch

        captcha_path = "/tmp/ecourts_captcha.png"
        try:
            captcha_img = self.page.locator("#captcha_image")
            await captcha_img.wait_for(timeout=10000)
            await captcha_img.screenshot(path=captcha_path)
            logger.info("Captcha image captured.")

            solved_text = captcha_solver.solve(captcha_path)
            logger.info(f"Captcha solved: '{solved_text}'")

            try:
                os.remove(captcha_path)
            except OSError:
                pass

            return solved_text if solved_text else None

        except Exception as e:
            logger.error(f"Captcha solving failed: {e}")
            return None

    async def _dismiss_modal(self):
        """Force-hide the #validateError Bootstrap modal via JavaScript."""
        try:
            await self.page.evaluate(
                """
                var m = document.getElementById('validateError');
                if (m) { m.style.display='none'; m.classList.remove('in'); }
                document.body.classList.remove('modal-open');
                var backdrop = document.querySelector('.modal-backdrop');
                if (backdrop) backdrop.remove();
                """
            )
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # Also try the close button
        try:
            close_btn = self.page.locator(
                "#validateError .close, #validateError button[data-dismiss='modal']"
            )
            if await close_btn.count() > 0:
                await close_btn.first.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass

    async def _refresh_captcha(self):
        """Dismiss any open modal then click the captcha refresh button."""
        await self._dismiss_modal()
        try:
            refresh_btn = self.page.locator(
                "a[onclick*='refreshCaptcha'], .captcha_refresh, #refresh_captcha"
            )
            if await refresh_btn.count() > 0:
                await refresh_btn.first.evaluate("el => el.click()")
                await asyncio.sleep(2)
                return
        except Exception:
            pass

        # Fallback: refresh image element
        try:
            refresh_imgs = self.page.locator("img[src*='refresh'], img[alt*='refresh']")
            if await refresh_imgs.count() > 0:
                await refresh_imgs.first.evaluate("el => el.click()")
                await asyncio.sleep(2)
        except Exception:
            logger.warning("Could not find captcha refresh button.")

    async def _check_captcha_error(self) -> bool:
        """Return True if the #validateError modal is currently visible."""
        try:
            display = await self.page.evaluate(
                """
                var el = document.getElementById('validateError');
                el ? window.getComputedStyle(el).display : 'none'
                """
            )
            return display == "block"
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  Search
    # ------------------------------------------------------------------ #

    async def search_petitioner(self, name: str, year: str = "") -> list[dict]:
        """
        Search for cases by petitioner name and year, solving the CAPTCHA.

        Args:
            name: Petitioner/respondent name (min 3 chars)
            year: Registration year (optional)

        Returns:
            List of enriched case detail dicts.
        """
        import time

        logger.info(f"==> Search start: petitioner='{name}' year='{year or 'all'}'")
        t0 = time.monotonic()

        await self.page.wait_for_selector("#petres_name", timeout=20000)
        await self.page.fill("#petres_name", name)

        if year:
            await self.page.fill("#rgyearP", str(year))

        # Ensure 'Both' is selected
        try:
            if not await self.page.is_checked("#radB"):
                await self.page.click("#radB")
        except Exception:
            pass

        # Captcha loop
        for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
            logger.info(f"Captcha attempt {attempt}/{MAX_CAPTCHA_RETRIES}")

            captcha_text = await self.solve_captcha()
            if not captcha_text:
                logger.warning("Captcha OCR returned empty. Refreshing...")
                await self._refresh_captcha()
                await asyncio.sleep(1)
                continue

            await self.page.fill("#fcaptcha_code", captcha_text)
            logger.info(f"Captcha filled: '{captcha_text}'. Clicking Go...")

            # Click the Go button — try multiple selectors with explicit waits
            clicked = False
            for selector in [
                "button:has-text('Go')",
                "button:has-text('go')",
                "input[type='submit']",
                "button.btn-primary",
                "button[type='submit']",
            ]:
                try:
                    btn = self.page.locator(selector).first
                    await btn.wait_for(state="visible", timeout=5000)
                    await btn.click()
                    logger.info(f"Clicked Go button via selector: {selector}")
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                logger.warning("Could not find Go button — trying JS submit")
                await self.page.evaluate("document.querySelector('form')?.submit()")

            await asyncio.sleep(3)

            if await self._check_captcha_error():
                logger.warning("Captcha incorrect. Refreshing and retrying...")
                await self._refresh_captcha()
                await asyncio.sleep(1)
                continue

            logger.info(
                f"Captcha accepted on attempt {attempt} ({time.monotonic() - t0:.1f}s). Parsing results..."
            )
            try:
                await self.page.screenshot(path="/tmp/ecourts_after_submit.png")
            except Exception:
                pass
            return await self.parse_results()

        msg = f"Failed to solve captcha after {MAX_CAPTCHA_RETRIES} retries ({time.monotonic() - t0:.1f}s elapsed)."
        logger.error(msg)
        raise RuntimeError(msg)

    # ------------------------------------------------------------------ #
    #  Results parsing
    # ------------------------------------------------------------------ #

    async def parse_results(self) -> list[dict]:
        """
        Parse the summary results table then fetch full case details for each row.
        """
        try:
            await asyncio.sleep(3)

            # If navigation opened a new page/tab, switch to it
            pages = self.context.pages
            if len(pages) > 1:
                self.page = pages[-1]
                logger.info(f"Switched to new page: {self.page.url}")
                await asyncio.sleep(2)

            summary_rows = await self._parse_summary_table()
            if not summary_rows:
                return []

            logger.info(
                f"Found {len(summary_rows)} case(s) in summary table. Fetching details..."
            )
            enriched = []
            for idx, summary in enumerate(summary_rows):
                import time as _time

                view_js = summary.pop("_view_js", None)
                case_ref = summary.get(
                    "Case Type/Case Number/Case Year", f"row {idx + 1}"
                )
                logger.info(
                    f"  [{idx + 1}/{len(summary_rows)}] Fetching detail: {case_ref}"
                )
                t_detail = _time.monotonic()
                detail = await self._fetch_detail_by_onclick(view_js) if view_js else {}
                logger.info(
                    f"  [{idx + 1}/{len(summary_rows)}] Detail fetched in {_time.monotonic() - t_detail:.1f}s ({len(detail)} fields)"
                )
                merged = {**summary, **detail} if detail else summary
                enriched.append(merged)
                await self._rate_limit_delay()

            logger.info(
                f"==> All details fetched: {len(enriched)} case record(s) complete."
            )
            return enriched

        except Exception as e:
            logger.error(f"Error parsing results: {e}")
            try:
                html = await self.page.content()
                with open("/tmp/ecourts_debug_page.html", "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info("Debug page saved to /tmp/ecourts_debug_page.html")
            except Exception:
                pass
            return []

    async def _parse_summary_table(self, html: str | None = None) -> list[dict]:
        """
        Parse the #dispTable results table.

        Table structure:
          <thead> — column headers in <th> cells (Sr No, Case Type/..., Petitioner..., View)
          <tbody> — mix of:
            * section header rows: <tr><th colspan="3" scope="colgroup">Court Name</th></tr>
            * data rows: <tr><td>1</td><td>R.C.A./181/2017</td><td>Petitioner</td><td><a onclick="viewHistory(...)">View</a></td></tr>

        Returns dicts with proper column names; View column replaced by "_view_js"
        containing the raw onclick JS string for direct evaluation.

        Args:
            html: Pre-fetched HTML string. If None, reads from the live browser page.
        """
        if html is None:
            html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # Check for "no records" message before attempting table parse
        no_record = soup.find(
            string=lambda t: (
                t and ("no record" in t.lower() or "no records" in t.lower())
            )
        )
        if no_record:
            logger.info("No records found for this search.")
            return []

        table = (
            soup.select_one("#dispTable")
            or soup.find("table", class_="table")
            or soup.find("table")
        )
        if not table:
            logger.warning("No results table found in page.")
            return []

        # ── Headers from <thead> ──────────────────────────────────────────
        headers: list[str] = []
        thead = table.find("thead")
        if thead:
            header_row = thead.find("tr")
            if header_row:
                headers = [h.get_text(strip=True) for h in header_row.find_all("th")]
        logger.info(f"Table headers: {headers}")

        # ── Data rows from <tbody> ────────────────────────────────────────
        tbody = table.find("tbody") or table
        for row in tbody.find_all("tr"):
            # Skip section-header rows (they have <th> cells, not <td>)
            if row.find("th"):
                continue

            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            row_data: dict = {}
            for i, cell in enumerate(cells):
                col_name = headers[i] if i < len(headers) else f"col_{i}"
                if col_name == "View":
                    a_tag = cell.find("a")
                    if a_tag and a_tag.get("onclick"):
                        row_data["_view_js"] = a_tag.get("onclick")
                else:
                    row_data[col_name] = cell.get_text(separator=" ", strip=True)

            if any(v for k, v in row_data.items() if k != "_view_js" and v):
                results.append(row_data)

        logger.info(f"Parsed {len(results)} rows from summary table.")
        return results

    async def _fetch_detail_by_onclick(self, view_js: str) -> dict:
        """
        Invoke viewHistory() via JS evaluate.

        viewHistory() is AJAX-based — it updates the DOM in-place without
        navigating, so the URL stays the same and viewHistory remains defined
        for subsequent calls. No go_back() needed.

        Falls back to new-tab handling if a new page is opened.
        """
        try:
            pages_before = len(self.context.pages)
            original_page = self.page

            await self.page.evaluate(view_js)
            await asyncio.sleep(3)

            # Check if a new tab was opened (non-AJAX fallback)
            current_pages = self.context.pages
            if len(current_pages) > pages_before:
                new_page = current_pages[-1]
                self.page = new_page
                await asyncio.sleep(2)
                detail = await self._parse_detail_page()
                await new_page.close()
                self.page = original_page
            else:
                # AJAX update — detail is in current DOM; don't navigate away
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                detail = await self._parse_detail_page()

            return detail

        except Exception as e:
            logger.error(f"Error fetching detail via onclick: {e}")
            return {}

    async def _parse_detail_page(self, html: str | None = None) -> dict:
        """
        Parse the case detail page using BeautifulSoup.

        Extracts: Case Type, Filing Number/Date, Registration Number/Date,
        CNR Number, e-Filing Number/Date, Under Act(s), First Hearing Date,
        Decision Date, Case Status, Nature of Disposal, Court Number and Judge,
        Petitioner_and_Advocate, Respondent_and_Advocate.

        Args:
            html: Pre-fetched HTML string. If None, reads from the live browser page.
        """
        detail: dict = {}
        try:
            if html is None:
                html = await self.page.content()
            soup = BeautifulSoup(html, "html.parser")

            def _normalize_fragment(text: str) -> str:
                """Normalize escaped HTML/text fragments returned by AJAX payloads."""
                if not text:
                    return ""
                cleaned = (
                    text.replace("\\/", "/")
                    .replace("\\\\n", "\n")
                    .replace("\\\\t", " ")
                    .replace("\\\\r", " ")
                    .replace("\\n", "\n")
                    .replace("\\t", " ")
                    .replace("\\r", " ")
                )
                cleaned = _html.unescape(cleaned).strip()
                if "<" in cleaned and ">" in cleaned:
                    cleaned = BeautifulSoup(cleaned, "html.parser").get_text(
                        separator="\n", strip=True
                    )
                lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
                return "\n".join(lines)

            def _clean_party_block(text: str, section: str) -> str:
                """Trim noisy tail sections from party/advocate blocks."""
                if not text:
                    return ""
                cleaned = text
                respondent_marker = "Respondent and Advocate"
                tail_markers = ("Acts", "Under Act(s)", "FIR Details", "Case History", "Processes")

                if section == "petitioner":
                    if respondent_marker in cleaned:
                        cleaned = cleaned.split(respondent_marker, 1)[0]
                else:
                    if respondent_marker in cleaned:
                        cleaned = cleaned.split(respondent_marker, 1)[1]

                for marker in tail_markers:
                    if marker in cleaned:
                        cleaned = cleaned.split(marker, 1)[0]
                return cleaned.strip()

            def get_field(label_text: str) -> str:
                """Find td/th with exact text; return text of the next sibling td."""
                for tag in soup.find_all(["td", "th"]):
                    if tag.get_text(strip=True) == label_text:
                        sibling = tag.find_next_sibling("td")
                        if sibling:
                            return _normalize_fragment(sibling.decode_contents())
                return ""

            def _norm_label(label: str) -> str:
                return _re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()

            kv: dict[str, str] = {}
            for row in soup.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) < 2:
                    continue
                label = _norm_label(cells[0].get_text(separator=" ", strip=True))
                value = _normalize_fragment(cells[1].decode_contents())
                if label and value and label not in kv:
                    kv[label] = value

            def get_field_fuzzy(*labels: str) -> str:
                for lbl in labels:
                    v = get_field(lbl)
                    if v:
                        return v
                targets = [_norm_label(lbl) for lbl in labels]
                for key, value in kv.items():
                    if any(key == t for t in targets):
                        return value
                return ""

            def _extract_from_flat_text(flat_text: str, label: str, all_labels: list[str]) -> str:
                escaped = [ _re.escape(lbl) for lbl in all_labels if lbl != label ]
                next_labels = "|".join(escaped)
                if not next_labels:
                    return ""
                pattern = rf"{_re.escape(label)}\s*(.*?)\s*(?=(?:{next_labels})\b|$)"
                m = _re.search(pattern, flat_text, flags=_re.IGNORECASE | _re.DOTALL)
                if not m:
                    return ""
                return _normalize_fragment(m.group(1))

            def _looks_merged(v: str) -> bool:
                if not v:
                    return False
                markers = ("Case Type", "Filing Number", "Registration Number", "CNR Number")
                return len(v) > 120 and sum(1 for mk in markers if mk in v) >= 2

            def _normalize_decision_date(value: str) -> str:
                """Keep decision date only when it looks like a real date token."""
                if not value:
                    return ""
                text = value.strip()
                # Common formats from eCourts pages.
                date_patterns = (
                    r"\b\d{1,2}-\d{1,2}-\d{4}\b",
                    r"\b\d{1,2}/\d{1,2}/\d{4}\b",
                    r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}\b",
                )
                for pat in date_patterns:
                    m = _re.search(pat, text, flags=_re.IGNORECASE)
                    if m:
                        return m.group(0)
                # If this contains other labels, it's likely spillover text.
                if any(
                    marker in text
                    for marker in ("Case Status", "Nature of Disposal", "Court Number", "Under Act")
                ):
                    return ""
                return ""

            detail["Case_Type"] = get_field_fuzzy("Case Type")
            detail["Filing_Number"] = get_field_fuzzy("Filing Number")
            detail["Filing_Date"] = get_field_fuzzy("Filing Date")
            detail["Registration_Number"] = get_field_fuzzy("Registration Number")
            detail["Registration_Date"] = get_field_fuzzy("Registration Date")
            detail["eFiling_Number"] = get_field_fuzzy("e-Filing Number", "e Filing Number")
            detail["eFiling_Date"] = get_field_fuzzy("e-Filing Date", "e Filing Date")

            # CNR Number — value is in a span.text-danger rather than the td text
            cnr_span = soup.select_one(
                "span.text-danger.text-uppercase, span.fw-bold.text-uppercase"
            )
            detail["CNR_Number"] = (
                cnr_span.get_text(strip=True) if cnr_span else get_field_fuzzy("CNR Number")
            )
            if detail.get("CNR_Number"):
                cnr_match = _re.search(r"\b[A-Z]{4}[A-Z0-9]{8,}\b", detail["CNR_Number"])
                if cnr_match:
                    detail["CNR_Number"] = cnr_match.group(0)

            # Under Act(s) — header is a th; data rows follow
            act_th = soup.find(
                lambda t: (
                    t.name in ("th", "td") and t.get_text(strip=True) == "Under Act(s)"
                )
            )
            if act_th:
                acts = []
                for row in act_th.find_parent("tr").find_next_siblings("tr"):
                    first_td = row.find("td")
                    if first_td:
                        text = first_td.get_text(strip=True)
                        if text:
                            acts.append(text)
                detail["Under_Acts"] = " | ".join(acts)

            detail["First_Hearing_Date"] = get_field_fuzzy("First Hearing Date")
            detail["Next_Hearing_Date"] = get_field_fuzzy("Next Hearing Date")
            detail["Case_Stage"] = get_field_fuzzy("Case Stage")
            detail["Decision_Date"] = get_field_fuzzy("Decision Date")
            detail["Case_Status"] = get_field_fuzzy("Case Status")
            detail["Nature_of_Disposal"] = get_field_fuzzy("Nature of Disposal")
            detail["Court_Number_Judge"] = get_field_fuzzy("Court Number and Judge")

            # Some responses collapse many key/value pairs into one giant text blob.
            # Re-split from flattened text to recover canonical field values.
            scalar_label_map = {
                "Case_Type": "Case Type",
                "Filing_Number": "Filing Number",
                "Filing_Date": "Filing Date",
                "Registration_Number": "Registration Number",
                "Registration_Date": "Registration Date",
                "CNR_Number": "CNR Number",
                "eFiling_Number": "e-Filing Number",
                "eFiling_Date": "e-Filing Date",
                "First_Hearing_Date": "First Hearing Date",
                "Next_Hearing_Date": "Next Hearing Date",
                "Case_Stage": "Case Stage",
                "Decision_Date": "Decision Date",
                "Case_Status": "Case Status",
                "Nature_of_Disposal": "Nature of Disposal",
                "Court_Number_Judge": "Court Number and Judge",
            }
            flat_text = _normalize_fragment(soup.get_text(separator="\n", strip=True))
            all_labels = list(scalar_label_map.values()) + [
                "Petitioner and Advocate",
                "Respondent and Advocate",
                "Under Act(s)",
            ]
            merged_detected = any(_looks_merged(detail.get(k, "")) for k in scalar_label_map)
            # Fallback when detail HTML is flattened/label-heavy and table extraction
            # yields sparse fields. Re-split values directly from flat text.
            populated_scalar_count = sum(1 for k in scalar_label_map if detail.get(k))
            if merged_detected or populated_scalar_count <= 2:
                for key, label in scalar_label_map.items():
                    extracted = _extract_from_flat_text(flat_text, label, all_labels)
                    if extracted:
                        detail[key] = extracted

            # Under_Acts often appears only in flattened payloads.
            if not detail.get("Under_Acts"):
                ua = _extract_from_flat_text(
                    flat_text,
                    "Under Act(s)",
                    all_labels + ["Under Section(s)", "FIR Details", "Case History", "Processes"],
                )
                if ua:
                    detail["Under_Acts"] = ua

            # Optional fields should remain empty when not explicitly present.
            # Avoid backfilling e-filing fields from filing fields.
            if detail.get("eFiling_Number") and detail.get("eFiling_Number") == detail.get("Filing_Number"):
                detail.pop("eFiling_Number", None)
            if detail.get("eFiling_Date") and detail.get("eFiling_Date") == detail.get("Filing_Date"):
                detail.pop("eFiling_Date", None)
            if detail.get("Decision_Date"):
                decision = _normalize_decision_date(detail["Decision_Date"])
                if decision:
                    detail["Decision_Date"] = decision
                else:
                    detail.pop("Decision_Date", None)

            # Normalize placeholder values to empty/missing.
            placeholders = {"-", "--", "na", "n/a", "not available", "nil", "null"}
            for k, v in list(detail.items()):
                if isinstance(v, str) and v.strip().lower() in placeholders:
                    detail.pop(k, None)

            # Petitioner / Respondent
            pet_ul = soup.select_one("ul.petitioner-advocate-list")
            if pet_ul:
                detail["Petitioner_and_Advocate"] = _clean_party_block(
                    _normalize_fragment(pet_ul.decode_contents()),
                    section="petitioner",
                )
            else:
                pet_from_field = get_field_fuzzy("Petitioner and Advocate")
                if pet_from_field:
                    detail["Petitioner_and_Advocate"] = _clean_party_block(
                        pet_from_field,
                        section="petitioner",
                    )

            res_ul = soup.select_one("ul.respondent-advocate-list")
            if res_ul:
                detail["Respondent_and_Advocate"] = _clean_party_block(
                    _normalize_fragment(res_ul.decode_contents()),
                    section="respondent",
                )
            else:
                res_from_field = get_field_fuzzy("Respondent and Advocate")
                if res_from_field:
                    detail["Respondent_and_Advocate"] = _clean_party_block(
                        res_from_field,
                        section="respondent",
                    )

            # Drop empty fields
            detail = {k: v for k, v in detail.items() if v}
            logger.info(f"Detail page parsed: {len(detail)} fields extracted.")

        except Exception as e:
            logger.error(f"Error parsing detail page: {e}")

        return detail

    # ------------------------------------------------------------------ #
    #  Phase 0 — Network discovery (TEMPORARY debug helper)
    # ------------------------------------------------------------------ #

    async def _dump_network(self, name: str, year: str):
        """
        TEMPORARY — Phase 0 network discovery.

        Intercepts every POST made during search_petitioner() and dumps the
        request URLs, bodies, and all hidden form fields to /tmp/ecourts_net.json.

        Usage (run once locally, then remove this method):
            scraper = ECourtsScraper(headless=False)
            await scraper.setup_driver()
            await scraper.navigate_and_select()
            await scraper._dump_network("Rajesh Gupta", "2017")
        """
        import json

        captured = []
        self.page.on(
            "request",
            lambda r: (
                captured.append(
                    {"url": r.url, "method": r.method, "post_data": r.post_data}
                )
                if r.method == "POST"
                else None
            ),
        )

        hidden = await self.page.evaluate(
            """() => {
                const f = document.querySelector('form');
                const out = {};
                if (f) f.querySelectorAll('input').forEach(i => {
                    if (i.name || i.id) out[i.name || i.id] = i.value;
                });
                return out;
            }"""
        )

        await self.search_petitioner(name, year)

        out_path = "/tmp/ecourts_net.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({"hidden_fields": hidden, "posts": captured}, fh, indent=2)

        logger.info(f"[Phase0] Network dump written to {out_path}")
        logger.info(f"[Phase0] hidden_fields: {list(hidden.keys())}")
        logger.info(f"[Phase0] POST requests captured: {len(captured)}")
        for p in captured:
            logger.info(f"  POST {p['url']!r}  body={str(p['post_data'])[:200]}")

    # ------------------------------------------------------------------ #
    #  Multi-year scraping
    # ------------------------------------------------------------------ #

    async def scrape_all_years(self, name: str) -> list[dict]:
        """Scrape all available years (current year back to 2000)."""
        all_results: list[dict] = []
        years = self._get_available_years()

        if not years:
            return await self.search_petitioner(name)

        logger.info(f"Scraping {len(years)} years...")

        for i, year in enumerate(years):
            logger.info(f"Scraping year {year} ({i + 1}/{len(years)})")

            if i > 0:
                await self.navigate_and_select()
                await self._rate_limit_delay()

            year_results = await self.search_petitioner(name, year)
            for r in year_results:
                r["Search_Year"] = year
            all_results.extend(year_results)
            logger.info(f"Year {year}: {len(year_results)} record(s) found.")

            if i < len(years) - 1:
                await self._rate_limit_delay()

        return all_results

    async def scrape_single_year(self, name: str, year: str) -> list[dict]:
        """Scrape cases for a specific year."""
        results = await self.search_petitioner(name, year)
        for r in results:
            r["Search_Year"] = year
        return results

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _get_available_years(self) -> list[str]:
        import datetime

        current_year = datetime.datetime.now().year
        return [str(y) for y in range(current_year, current_year - 15, -1)]

    async def _rate_limit_delay(self):
        delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
        logger.info(f"Rate limit delay: {delay:.1f}s")
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    #  Export
    # ------------------------------------------------------------------ #

    @staticmethod
    def export_to_csv(data: list[dict], filename: str = "results.csv"):
        """Export scraped data to CSV file."""
        if not data:
            logger.warning("No data to export.")
            return
        df = pd.DataFrame(data)
        expected_cols = [
            "Sr No",
            "Case Type/Case Number/Case Year",
            "Petitioner Name versus Respondent Name",
            "CNR_Number",
            "Case_Type",
            "Filing_Number",
            "Filing_Date",
            "Registration_Number",
            "Registration_Date",
            "eFiling_Number",
            "eFiling_Date",
            "Under_Acts",
            "First_Hearing_Date",
            "Next_Hearing_Date",
            "Case_Stage",
            "Decision_Date",
            "Case_Status",
            "Nature_of_Disposal",
            "Court_Number_Judge",
            "Petitioner_and_Advocate",
            "Respondent_and_Advocate",
            "Search_Year",
        ]
        for col in expected_cols:
            if col not in df.columns:
                df[col] = ""
        df = df[expected_cols + [c for c in df.columns if c not in expected_cols]]
        df.to_csv(filename, index=False, encoding="utf-8-sig")
        logger.info(f"Data exported to {filename} ({len(data)} records)")


# ── Hybrid Browser + HTTP Scraper ────────────────────────────────────────────


class HybridECourtsScraper(ECourtsScraper):
    """
    Optimised scraper: opens a browser exactly once to establish a PHP session,
    then performs all year searches via plain httpx POST requests.

    Expected gain: ~35% faster end-to-end. Chromium runs only ~10-15s (one page
    load to get PHPSESSID) instead of 15+ minutes (15 full navigations).

    Per-year flow (HTTP-only after bootstrap):
      1. GET captcha image   → solve with RapidOCR         (~0.5s)
      2. POST submitPartyName → parse summary HTML          (~2s)
      3. POST viewHistory × N → parse each detail fragment  (~2s × N)
    """

    SESSION_TTL = 1200  # 20 min; PHP gc_maxlifetime default is 24 min

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._session: Optional[ScrapingSession] = None
        self._http: Optional[httpx.AsyncClient] = None

    # ── One-time setup ─────────────────────────────────────────────────────

    async def setup_driver(self):
        """No-op: Hybrid bootstraps lazily inside scrape_all_years."""
        pass

    async def navigate_and_select(self):
        """No-op: court selection is baked into the POST body, not navigation."""
        pass

    # ── Session bootstrap (browser, runs once per scrape) ──────────────────

    async def bootstrap_session(self) -> ScrapingSession:
        """
        Open Chromium once to extract session cookies and hidden form fields:
          1. navigate_and_select  (state → district → court complex AJAX)
          2. Extract SERVICES_SESSID + JSESSION cookies from browser context
          3. Extract app_token hidden field from the search form
          4. Close browser

        No form submission needed — captcha is fetched fresh per year via HTTP.
        """
        logger.info("[Hybrid] Bootstrapping session via browser...")
        t0 = time.monotonic()

        await super().setup_driver()
        services_sessid = ""
        jsession = ""
        app_token = ""
        try:
            await super().navigate_and_select()

            ctx = self.context
            assert ctx is not None, "Browser context not initialised"
            cookies = await ctx.cookies()
            cookie_map = {c.get("name", ""): c.get("value", "") for c in cookies}
            logger.info(f"[Hybrid] Cookies found: {list(cookie_map.keys())}")

            services_sessid = cookie_map.get("SERVICES_SESSID", "")
            jsession = cookie_map.get("JSESSION", "")

            if not services_sessid:
                raise RuntimeError(
                    f"[Hybrid] SERVICES_SESSID not found. "
                    f"Available cookies: {list(cookie_map.keys())}"
                )

            pg = self.page
            assert pg is not None
            app_token = await pg.evaluate(
                "() => document.getElementById('app_token')?.value || ''"
            )
        finally:
            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass
            pw = self._playwright
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
            self.browser = None
            self._playwright = None
            self.context = None
            self.page = None

        self._session = ScrapingSession(
            services_sessid=services_sessid,
            jsession=jsession,
            app_token=app_token,
        )
        logger.info(
            f"[Hybrid] Session bootstrapped in {time.monotonic() - t0:.1f}s. "
            f"SERVICES_SESSID={services_sessid[:8]}..."
        )
        return self._session

    def _session_is_fresh(self) -> bool:
        return (
            self._session is not None
            and (time.monotonic() - self._session.created_at) < self.SESSION_TTL
        )

    # ── HTTP client ────────────────────────────────────────────────────────

    async def _open_http_client(self):
        """Open/rotate the persistent httpx client (reused for all year searches)."""
        assert self._session is not None
        if self._http and not self._http.is_closed:
            await self._http.aclose()
            self._http = None
        # Build Cookie header directly — more reliable than httpx cookie jar
        # for server-set session cookies with implicit domain/path.
        cookie_parts = [f"SERVICES_SESSID={self._session.services_sessid}"]
        if self._session.jsession:
            cookie_parts.append(f"JSESSION={self._session.jsession}")
        cookie_header = "; ".join(cookie_parts)

        headers = {**_HTTP_HEADERS, "Cookie": cookie_header}
        logger.info(f"[Hybrid] HTTP client cookie: {cookie_header}")

        self._http = httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=15.0),
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _unwrap_ajax_html(raw: str) -> str:
        """
        AJAX endpoints wrap HTML inside a JSON envelope.
        Try common key names; fall back to raw text if not JSON or no known key.
        """
        key_order = ("party_data", "tab_data", "html", "data", "case_history")
        try:
            data = _json.loads(raw)
            if isinstance(data, dict):
                for key in key_order:
                    if key in data and isinstance(data[key], str):
                        return data[key]
        except Exception:
            # Some responses are almost-JSON strings that fail strict parsing.
            # Best-effort extract the key payload and decode escapes.
            for key in key_order:
                m = _re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', raw)
                if m:
                    payload = m.group(1)
                    try:
                        return _json.loads(f'"{payload}"')
                    except Exception:
                        cleaned = (
                            payload.replace("\\/", "/")
                            .replace("\\n", "\n")
                            .replace("\\t", " ")
                            .replace("\\r", " ")
                        )
                        return _html.unescape(cleaned)
        return raw

    # ── HTTP captcha solve ─────────────────────────────────────────────────

    async def _fetch_captcha_http(self, client: httpx.AsyncClient) -> Optional[str]:
        """
        Refresh the server-side captcha, download the image, and solve it.

        Two-step process (mirroring what the browser does):
          1. POST getCaptcha  → server generates a captcha and returns the exact
                                image URL with a namespace hash, e.g.
                                securimage_show.php?c87fa13d7b5fa8d0761b64...
          2. GET that exact URL → download the image for OCR

        The hash in the URL is the Securimage namespace key — we MUST use the
        same URL the server returned, or we'll solve a captcha from a different
        namespace than what submitPartyName validates against.
        """
        import captcha_solver

        captcha_path = "/tmp/ecourts_http_captcha.png"
        for fetch_attempt in range(1, CAPTCHA_FETCH_RETRIES + 1):
            try:
                # Step 1: trigger captcha generation, get the exact image URL
                cap_resp = await client.post(
                    _GET_CAPTCHA_URL,
                    data={"ajax_req": "true", "app_token": ""},
                )
                logger.info(
                    f"[Hybrid] getCaptcha: {cap_resp.status_code} "
                    f"{len(cap_resp.text)} bytes"
                )

                # Parse the JSON response to extract the captcha image src
                captcha_url = None
                try:
                    data = _json.loads(cap_resp.text)
                    div_html = data.get("div_captcha", "")
                    # src is JSON-escaped: src=\"\/ecourtindia_v6\/vendor\/...\"
                    m = _re.search(r'src=["\']([^"\']*securimage[^"\']*)["\']', div_html)
                    if m:
                        raw_path = m.group(1).replace("\\/", "/")
                        captcha_url = f"https://services.ecourts.gov.in{raw_path}"
                        logger.info(f"[Hybrid] Captcha URL from getCaptcha: {captcha_url}")
                except Exception as parse_err:
                    logger.warning(f"[Hybrid] Could not parse getCaptcha JSON: {parse_err}")

                # Fallback: use the default securimage URL if parsing failed
                if not captcha_url:
                    captcha_url = f"{_CAPTCHA_URL}?t={int(time.monotonic() * 1000)}"
                    logger.warning("[Hybrid] Falling back to default captcha URL")

                # Step 2: download the exact captcha image the server generated
                resp = await client.get(captcha_url)
                resp.raise_for_status()
                with open(captcha_path, "wb") as fh:
                    fh.write(resp.content)
                solved = captcha_solver.solve(captcha_path)
                logger.info(f"[Hybrid] HTTP captcha solved: '{solved}'")
                return solved or None
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.warning(
                    f"[Hybrid] HTTP captcha fetch transient error "
                    f"({fetch_attempt}/{CAPTCHA_FETCH_RETRIES}): {e}"
                )
                if fetch_attempt < CAPTCHA_FETCH_RETRIES:
                    await asyncio.sleep(min(2 * fetch_attempt, 5))
                    continue
                logger.error(f"[Hybrid] HTTP captcha fetch failed after retries: {e}")
                return None
            except Exception as e:
                logger.error(f"[Hybrid] HTTP captcha fetch failed: {e}")
                return None
        return None

    # ── Core HTTP search ───────────────────────────────────────────────────

    async def _http_search_year(
        self, client: httpx.AsyncClient, name: str, year: str
    ) -> list[dict]:
        """Submit party name search via HTTP and return enriched case records."""
        last_error: str | None = None
        for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
            captcha_text = await self._fetch_captcha_http(client)
            if not captcha_text:
                logger.warning(f"[Hybrid] Captcha attempt {attempt} empty, retrying...")
                continue

            try:
                resp = await client.post(
                    _SEARCH_URL,
                    data={
                        # Exact field names/values captured from live browser POST
                        "petres_name": name,
                        "rgyearP": year,
                        "case_status": "Both",
                        "fcaptcha_code": captcha_text,
                        "state_code": _STATE_CODE,
                        "dist_code": _DIST_CODE,
                        "court_complex_code": _COURT_COMPLEX_BARE,  # bare "1010303", not the @-value
                        "est_code": "null",
                        "ajax_req": "true",
                        "app_token": "",
                    },
                )
                resp.raise_for_status()
            except httpx.TimeoutException:
                last_error = (
                    f"Year {year}: submitPartyName timed out on attempt "
                    f"{attempt}/{MAX_CAPTCHA_RETRIES}"
                )
                logger.warning(f"[Hybrid] {last_error}")
                await asyncio.sleep(min(2 * attempt, 8))
                continue
            except httpx.RequestError as e:
                last_error = (
                    f"Year {year}: submitPartyName request error on attempt "
                    f"{attempt}/{MAX_CAPTCHA_RETRIES}: {e!r}"
                )
                logger.warning(f"[Hybrid] {last_error}")
                await asyncio.sleep(min(2 * attempt, 8))
                continue

            # Debug: log response details; save artifacts only when explicitly enabled.
            ct = resp.headers.get("content-type", "?")
            logger.info(
                f"[Hybrid] submitPartyName: HTTP {resp.status_code} | "
                f"{len(resp.content)} bytes | content-type: {ct}"
            )
            if resp.content:
                logger.info(f"[Hybrid] Response preview: {resp.text[:400]!r}")
            else:
                logger.warning("[Hybrid] EMPTY response body — session/cookie problem?")
            if DEBUG_ARTIFACTS:
                debug_path = f"/tmp/ecourts_resp_{year}_attempt{attempt}.html"
                with open(debug_path, "wb") as _f:
                    _f.write(resp.content)
                logger.info(f"[Hybrid] Saved debug artifact: {debug_path}")

            # Unwrap JSON envelope: {"party_data": "<html>", "status": 1, "div_captcha": "..."}
            # The server returns AJAX JSON, not raw HTML.
            html = self._unwrap_ajax_html(resp.text)
            logger.info(f"[Hybrid] HTML for parsing: {len(html)} chars")

            _lower = html.lower()

            # Check captcha rejection FIRST — these responses contain
            # "securimage_show" in the div_captcha HTML, so must be checked
            # before the session expiry detector to avoid false positives.
            if (
                "invalid captcha" in _lower
                or "incorrect captcha" in _lower
                or "validateerror" in _lower
                or ('"errormsg"' in resp.text and "captcha" in _lower)
            ):
                logger.warning(
                    f"[Hybrid] Captcha rejected on attempt {attempt}, retrying..."
                )
                continue

            # Session expiry: look for actual auth/redirect indicators only
            if any(
                marker in _lower
                for marker in (
                    "session expired",
                    "please login",
                    "login required",
                    "invalid session",
                )
            ):
                raise SessionExpiredError(
                    f"[Hybrid] Year {year}: session expiry detected in response"
                )

            rows = await self._parse_summary_table(html=html)
            logger.info(f"[Hybrid] Year {year}: {len(rows)} row(s)")

            enriched = []
            for idx, summary in enumerate(rows):
                view_js = summary.pop("_view_js", None)
                case_ref = summary.get(
                    "Case Type/Case Number/Case Year", f"row {idx + 1}"
                )
                logger.info(
                    f"  [Hybrid] [{idx + 1}/{len(rows)}] Fetching detail: {case_ref}"
                )
                detail = (
                    await self._http_fetch_detail(client, view_js) if view_js else {}
                )
                enriched.append({**summary, **detail} if detail else summary)
                await self._rate_limit_delay()

            return enriched

        if last_error:
            raise RuntimeError(
                f"[Hybrid] Failed to scrape year {year} after "
                f"{MAX_CAPTCHA_RETRIES} attempts. Last error: {last_error}"
            )
        raise RuntimeError(
            f"[Hybrid] Failed to solve captcha after {MAX_CAPTCHA_RETRIES} "
            f"attempts for year {year}"
        )

    async def _http_fetch_detail(self, client: httpx.AsyncClient, view_js: str) -> dict:
        """
        Parse viewHistory() onclick and fetch case detail via HTTP POST.

        Arg order (discovered in Phase 0):
          viewHistory(case_no, cino, court_code, hideparty, search_flag,
                      state_code, dist_code, court_complex_code, search_by)
        """
        import re

        m = re.search(r"viewHistory\(([^)]+)\)", view_js)
        if not m:
            return {}
        args = [a.strip().strip("'\"") for a in m.group(1).split(",")]
        if len(args) < 3:
            return {}

        s = self._session
        try:
            resp = await client.post(
                _VIEW_HISTORY_URL,
                data={
                    "court_code": args[2],
                    "state_code": args[5] if len(args) > 5 else _STATE_CODE,
                    "dist_code": args[6] if len(args) > 6 else _DIST_CODE,
                    "court_complex_code": args[7]
                    if len(args) > 7
                    else _COURT_COMPLEX_CODE,
                    "case_no": args[0],
                    "cino": args[1],
                    "hideparty": args[3] if len(args) > 3 else "",
                    "search_flag": args[4] if len(args) > 4 else "CScaseNumber",
                    "search_by": args[8] if len(args) > 8 else "CSpartyName",
                    "ajax_req": "true",
                    "app_token": s.app_token,
                },
            )
            resp.raise_for_status()
            html = self._unwrap_ajax_html(resp.text)
            return await self._parse_detail_page(html=html)
        except Exception as e:
            logger.error(f"[Hybrid] viewHistory fetch failed: {e}")
            return {}

    # ── Public overrides ────────────────────────────────────────────────────

    async def scrape_all_years(self, name: str) -> list[dict]:
        """Bootstrap session once via browser, then HTTP for all 15 years."""
        await self.bootstrap_session()
        maybe_client_open = self._open_http_client()
        if asyncio.iscoroutine(maybe_client_open):
            await maybe_client_open
        http = self._http
        assert http is not None

        years = self._get_available_years()
        all_results: list[dict] = []

        for i, year in enumerate(years):
            if not self._session_is_fresh():
                logger.info("[Hybrid] Session stale — re-bootstrapping...")
                await self.bootstrap_session()
                maybe_client_open = self._open_http_client()
                if asyncio.iscoroutine(maybe_client_open):
                    await maybe_client_open
                http = self._http
                assert http is not None

            logger.info(f"[Hybrid] Scraping year {year} ({i + 1}/{len(years)}) [HTTP]")
            try:
                rows = await self._http_search_year(http, name, year)
            except SessionExpiredError:
                logger.warning(
                    f"[Hybrid] Session expired on year {year}, re-bootstrapping..."
                )
                await self.bootstrap_session()
                maybe_client_open = self._open_http_client()
                if asyncio.iscoroutine(maybe_client_open):
                    await maybe_client_open
                http = self._http
                assert http is not None
                rows = await self._http_search_year(http, name, year)

            for r in rows:
                r["Search_Year"] = year
            all_results.extend(rows)
            logger.info(f"[Hybrid] Year {year}: {len(rows)} record(s)")
            await self._rate_limit_delay()

        return all_results

    async def search_petitioner(self, name: str, year: str = "") -> list[dict]:
        """Single-year search: bootstrap via browser if needed, then HTTP."""
        if self._http is None or self._http.is_closed or not self._session_is_fresh():
            await self.bootstrap_session()
            maybe_client_open = self._open_http_client()
            if asyncio.iscoroutine(maybe_client_open):
                await maybe_client_open

        http = self._http
        assert http is not None
        try:
            return await self._http_search_year(http, name, year)
        except SessionExpiredError as e:
            logger.warning(f"{e} — re-bootstrapping session...")
            await self.bootstrap_session()
            maybe_client_open = self._open_http_client()
            if asyncio.iscoroutine(maybe_client_open):
                await maybe_client_open
            http = self._http
            assert http is not None
            return await self._http_search_year(http, name, year)

    async def close(self):
        """Close the persistent HTTP client and any open browser."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        logger.info("[Hybrid] Scraper closed.")
