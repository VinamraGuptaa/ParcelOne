"""
eCourts India Case Status Scraper.

Scrapes case data by petitioner name from the eCourts India portal.
Pre-configured for: Maharashtra → Pune → Pune District and Sessions Court.

Rewritten with Playwright (async) — replaces the original Selenium implementation.
Public API is identical; all methods are now async.
"""

import asyncio
import os
import random
import logging
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext, Browser
from bs4 import BeautifulSoup
import pandas as pd

# captcha_solver is imported lazily inside solve_captcha() to defer torch
# loading until after Chromium is already running (saves ~400MB at launch time)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/index&app_token="

STATE_TEXT = "Maharashtra"
DISTRICT_TEXT = "Pune"
COURT_COMPLEX_TEXT = "Pune, District and Sessions Court"

MIN_DELAY_SECONDS = 3
MAX_DELAY_SECONDS = 7
MAX_CAPTCHA_RETRIES = 5


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
        browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "~/.cache/ms-playwright")
        logger.info(f"Launching Playwright Chromium (headless={self.headless}, browsers_path={browsers_path})...")
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
        logger.info(f"Navigating to eCourts portal...")
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
        await self._wait_for_option_containing("#court_complex_code", COURT_COMPLEX_TEXT)

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

    async def _wait_for_option_containing(self, selector: str, text: str, timeout_s: int = 15):
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
            f"Option containing '{text}' not found in {selector}. "
            f"Available: {options}"
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
            refresh_imgs = self.page.locator(
                "img[src*='refresh'], img[alt*='refresh']"
            )
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
                await self.page.evaluate(
                    "document.querySelector('form')?.submit()"
                )

            await asyncio.sleep(3)

            if await self._check_captcha_error():
                logger.warning("Captcha incorrect. Refreshing and retrying...")
                await self._refresh_captcha()
                await asyncio.sleep(1)
                continue

            logger.info(f"Captcha accepted on attempt {attempt} ({time.monotonic() - t0:.1f}s). Parsing results...")
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

            logger.info(f"Found {len(summary_rows)} case(s) in summary table. Fetching details...")
            enriched = []
            for idx, summary in enumerate(summary_rows):
                import time as _time
                view_js = summary.pop("_view_js", None)
                case_ref = summary.get('Case Type/Case Number/Case Year', f'row {idx+1}')
                logger.info(f"  [{idx+1}/{len(summary_rows)}] Fetching detail: {case_ref}")
                t_detail = _time.monotonic()
                detail = await self._fetch_detail_by_onclick(view_js) if view_js else {}
                logger.info(f"  [{idx+1}/{len(summary_rows)}] Detail fetched in {_time.monotonic() - t_detail:.1f}s ({len(detail)} fields)")
                merged = {**summary, **detail} if detail else summary
                enriched.append(merged)
                await self._rate_limit_delay()

            logger.info(f"==> All details fetched: {len(enriched)} case record(s) complete.")
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

    async def _parse_summary_table(self) -> list[dict]:
        """
        Parse the #dispTable results table.

        Table structure:
          <thead> — column headers in <th> cells (Sr No, Case Type/..., Petitioner..., View)
          <tbody> — mix of:
            * section header rows: <tr><th colspan="3" scope="colgroup">Court Name</th></tr>
            * data rows: <tr><td>1</td><td>R.C.A./181/2017</td><td>Petitioner</td><td><a onclick="viewHistory(...)">View</a></td></tr>

        Returns dicts with proper column names; View column replaced by "_view_js"
        containing the raw onclick JS string for direct evaluation.
        """
        html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # Check for "no records" message before attempting table parse
        no_record = soup.find(
            string=lambda t: t and (
                "no record" in t.lower() or "no records" in t.lower()
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

    async def _parse_detail_page(self) -> dict:
        """
        Parse the case detail page using BeautifulSoup.

        Extracts: Case Type, Filing Number/Date, Registration Number/Date,
        CNR Number, e-Filing Number/Date, Under Act(s), First Hearing Date,
        Decision Date, Case Status, Nature of Disposal, Court Number and Judge,
        Petitioner_and_Advocate, Respondent_and_Advocate.
        """
        detail: dict = {}
        try:
            html = await self.page.content()
            soup = BeautifulSoup(html, "html.parser")

            def get_field(label_text: str) -> str:
                """Find td/th with exact text; return text of the next sibling td."""
                for tag in soup.find_all(["td", "th"]):
                    if tag.get_text(strip=True) == label_text:
                        sibling = tag.find_next_sibling("td")
                        if sibling:
                            return sibling.get_text(strip=True)
                return ""

            detail["Case_Type"]           = get_field("Case Type")
            detail["Filing_Number"]       = get_field("Filing Number")
            detail["Filing_Date"]         = get_field("Filing Date")
            detail["Registration_Number"] = get_field("Registration Number")
            detail["Registration_Date"]   = get_field("Registration Date")
            detail["eFiling_Number"]      = get_field("e-Filing Number")
            detail["eFiling_Date"]        = get_field("e-Filing Date")

            # CNR Number — value is in a span.text-danger rather than the td text
            cnr_span = soup.select_one(
                "span.text-danger.text-uppercase, span.fw-bold.text-uppercase"
            )
            detail["CNR_Number"] = (
                cnr_span.get_text(strip=True) if cnr_span else get_field("CNR Number")
            )

            # Under Act(s) — header is a th; data rows follow
            act_th = soup.find(
                lambda t: t.name in ("th", "td")
                and t.get_text(strip=True) == "Under Act(s)"
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

            detail["First_Hearing_Date"] = get_field("First Hearing Date")
            detail["Next_Hearing_Date"]  = get_field("Next Hearing Date")
            detail["Case_Stage"]         = get_field("Case Stage")
            detail["Decision_Date"]      = get_field("Decision Date")
            detail["Case_Status"]        = get_field("Case Status")
            detail["Nature_of_Disposal"] = get_field("Nature of Disposal")
            detail["Court_Number_Judge"] = get_field("Court Number and Judge")

            # Petitioner / Respondent
            pet_ul = soup.select_one("ul.petitioner-advocate-list")
            if pet_ul:
                detail["Petitioner_and_Advocate"] = pet_ul.get_text(
                    separator="\n", strip=True
                )

            res_ul = soup.select_one("ul.respondent-advocate-list")
            if res_ul:
                detail["Respondent_and_Advocate"] = res_ul.get_text(
                    separator="\n", strip=True
                )

            # Drop empty fields
            detail = {k: v for k, v in detail.items() if v}
            logger.info(f"Detail page parsed: {len(detail)} fields extracted.")

        except Exception as e:
            logger.error(f"Error parsing detail page: {e}")

        return detail

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
        df.to_csv(filename, index=False, encoding="utf-8-sig")
        logger.info(f"Data exported to {filename} ({len(data)} records)")
