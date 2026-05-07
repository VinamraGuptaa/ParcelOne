"""
Maharashtra Bhulekh (NewBhulekh.aspx) scraper — Playwright + RapidOCR captcha.

Flow mirrors the live site: district → taluka → village → survey type →
part-1 search → survey dropdown → mobile → English → captcha → submit.

Rate limiting uses the same randomized delay band as eCourts (`scraper.py`).
"""

from __future__ import annotations

import asyncio
import unicodedata
import base64
import dataclasses
import difflib
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import sys
import time
from urllib.parse import urljoin, urlparse
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from playwright_launch_args import chromium_launch_args

# Bhulekh previously used 3-7s random sleeps per interaction, which makes one run
# very slow because this helper is called many times in sequence. Keep anti-burst
# jitter, but default to a short range and allow env overrides when needed.
MIN_DELAY_SECONDS = float(os.getenv("BHULEKH_MIN_DELAY_SECONDS", "0.25"))
MAX_DELAY_SECONDS = float(os.getenv("BHULEKH_MAX_DELAY_SECONDS", "0.9"))

BASE_URL = "https://bhulekh.mahabhumi.gov.in/NewBhulekh.aspx"

MAX_CAPTCHA_RETRIES = 10

# Stable selectors from NewBhulekh.aspx (ContentPlaceHolder1)
SEL_DIST = "#ContentPlaceHolder1_ddlMainDist"
SEL_TALUKA = "#ContentPlaceHolder1_ddlTalForAll"
SEL_VILLAGE = "#ContentPlaceHolder1_ddlVillForAll"
SEL_SURVEY_TYPE = "#ContentPlaceHolder1_ddlSelectSearchType"
SEL_SURVEY_NO = "#ContentPlaceHolder1_ddlsurveyno"
TXT_SURVEY_PART1 = "#ContentPlaceHolder1_txtcsno"
BTN_FIND_SURVEY = "#ContentPlaceHolder1_btnsearchfind"
TXT_MOBILE = "#ContentPlaceHolder1_txtmobile1"
SEL_LANG = "#ContentPlaceHolder1_ddllangforAll"
TXT_CAPTCHA = "#ContentPlaceHolder1_txtcaptcha"
IMG_CAPTCHA = "#ContentPlaceHolder1_captchaImage"
BTN_REFRESH_CAPTCHA = "#ContentPlaceHolder1_btnreferesh"
BTN_SUBMIT = "#ContentPlaceHolder1_btnmainsubmit"
UPDATE_PROGRESS = "#ContentPlaceHolder1_UpdateProgress11"

# After submit, the 7/12 (or related record) renders inside one of these panels — not a new URL.
RESULT_PANEL_SELECTORS: tuple[str, ...] = (
    "#ContentPlaceHolder1_showPopUp",
    "#ContentPlaceHolder1_show8a",
    "#ContentPlaceHolder1_showreport",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
_DEBUG_LOG_PATH = "/Users/vinamragupta/.gemini/antigravity/playground/icy-disk/.cursor/debug-eb113b.log"


def _debug_log(hypothesis_id: str, message: str, data: dict, run_id: str = "bhulekh") -> None:
    # #region agent log
    payload = {
        "sessionId": "eb113b",
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": "bhulekh_scraper.py",
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass
    # #endregion

# Progress markers for operator logs (aligned with run_search / run_search_with_labels)
STEP_LOAD = "1/9"
STEP_DISTRICT = "2/9"
STEP_TALUKA = "3/9"
STEP_VILLAGE = "4/9"
STEP_SURVEY_TYPE = "5/9"
STEP_SURVEY_PART1 = "6/9"
STEP_SURVEY_NO = "7/9"
STEP_CAPTCHA_SUBMIT = "8/9"
STEP_DONE = "9/9"


async def rate_limit_delay() -> None:
    """Random pause to reduce burst traffic with configurable jitter."""
    hi = max(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
    lo = min(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
    if hi <= 0:
        return
    d = random.uniform(lo, hi)
    logger.info("Rate limit delay: %.2fs", d)
    await asyncio.sleep(d)


def _extract_options_from_select_html(fragment: str, select_id: str) -> list[dict[str, str]]:
    """Parse <option> list from HTML (unit-testable without Playwright)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(fragment, "html.parser")
    sel = soup.find("select", id=select_id)
    if not sel:
        return []
    out = []
    for opt in sel.find_all("option"):
        val = (opt.get("value") or "").strip()
        label = opt.get_text(strip=True)
        out.append({"value": val, "label": label})
    return out


@dataclasses.dataclass
class BhulekhSearchParams:
    """Search by survey (7/12) when ULPIN is not used."""

    district_value: str
    taluka_value: str
    village_value: str
    survey_part1: str
    survey_number_value: str
    mobile: str = "9999999999"
    survey_type_option_value: str = "2"
    language_value: str = "en_in"


class BhulekhScraper:
    """Playwright-based automation for bhulekh.mahabhumi.gov.in (7/12 by survey)."""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def setup_driver(self) -> None:
        def _resolve_chromium_executable() -> str | None:
            explicit = (os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or "").strip()
            if explicit and Path(explicit).exists():
                return explicit
            return None

        def _resolve_system_chromium_executable() -> str | None:
            candidates = (
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            )
            for c in candidates:
                if Path(c).exists():
                    return c
            return None

        project_browsers_path = str((Path(__file__).resolve().parent / ".playwright-browsers").resolve())
        browsers_path_env = (os.getenv("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
        if not browsers_path_env or "cursor-sandbox-cache" in browsers_path_env:
            # Use a repo-local path that stays writable/stable across Cursor
            # sandbox hash changes and avoids user-home permission edge cases.
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = project_browsers_path
            logger.warning(
                "Using PLAYWRIGHT_BROWSERS_PATH=%s (previous=%r)",
                project_browsers_path,
                browsers_path_env or None,
            )

        async def _install_playwright_chromium(*, original_error: str = "") -> None:
            auto_install = (os.getenv("PLAYWRIGHT_AUTO_INSTALL") or "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            if not auto_install:
                raise RuntimeError(
                    "Playwright Chromium binary is missing and runtime auto-install is disabled. "
                    "Run once: PLAYWRIGHT_BROWSERS_PATH=\"$PWD/.playwright-browsers\" "
                    ".venv/bin/python -m playwright install chromium "
                    "and restart backend. (Set PLAYWRIGHT_AUTO_INSTALL=1 to allow runtime install.) "
                    f"Original launch error: {original_error}"
                )

            async def _run_install_once() -> tuple[int, str]:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "playwright",
                    "install",
                    "chromium",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await proc.communicate()
                detail = (err or out or b"").decode("utf-8", errors="ignore").strip()
                return proc.returncode, detail

            logger.warning(
                "Playwright Chromium binary missing; running '%s -m playwright install chromium' (PLAYWRIGHT_BROWSERS_PATH=%s).",
                sys.executable,
                os.getenv("PLAYWRIGHT_BROWSERS_PATH"),
            )
            code, detail = await _run_install_once()
            if code == 0:
                logger.info("Playwright Chromium install completed.")
                return
            if "eperm" in detail.lower() and "__dirlock" in detail.lower():
                lock_path = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) / "__dirlock"
                try:
                    shutil.rmtree(lock_path, ignore_errors=True)
                    if lock_path.exists():
                        lock_path.unlink(missing_ok=True)
                except Exception:
                    pass
                logger.warning("Playwright dir lock cleanup attempted at %s; retrying install once.", lock_path)
                code, detail = await _run_install_once()
                if code == 0:
                    logger.info("Playwright Chromium install completed after dirlock cleanup.")
                    return
            raise RuntimeError(f"Playwright install chromium failed (exit {code}): {detail}")

        def _is_gl_launch_failure(exc: Exception) -> bool:
            msg = str(exc).lower()
            needles = (
                "egl_not_initialized",
                "vk_ext_metal_surface",
                "vk_khr_surface",
                "gldisplayegl::initialize failed",
                "target page, context or browser has been closed",
            )
            return any(n in msg for n in needles)

        def _is_missing_executable_error(exc: Exception) -> bool:
            msg = str(exc).lower()
            return "executable doesn't exist" in msg or "playwright install" in msg

        self._playwright = await async_playwright().start()
        allow_headed_fallback = (os.getenv("PLAYWRIGHT_ALLOW_HEADED_FALLBACK") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        explicit_executable_path = _resolve_chromium_executable()
        playwright_executable_path = (self._playwright.chromium.executable_path or "").strip()
        executable_path = explicit_executable_path or (playwright_executable_path if Path(playwright_executable_path).exists() else None)
        if explicit_executable_path:
            logger.info("Using explicit Chromium executable at %s", explicit_executable_path)
        elif executable_path:
            logger.info("Using Playwright Chromium executable at %s", executable_path)
        browser_home = (Path(__file__).resolve().parent / ".playwright-home").resolve()
        browser_tmp = (Path(__file__).resolve().parent / ".playwright-tmp").resolve()
        browser_home.mkdir(parents=True, exist_ok=True)
        browser_tmp.mkdir(parents=True, exist_ok=True)
        browser_env = {
            **os.environ,
            "HOME": str(browser_home),
            "TMPDIR": str(browser_tmp),
            "XDG_CONFIG_HOME": str(browser_home / ".config"),
            "XDG_CACHE_HOME": str(browser_home / ".cache"),
        }
        launch_args = chromium_launch_args()
        try:
            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                "args": launch_args,
                "env": browser_env,
            }
            if executable_path:
                launch_kwargs["executable_path"] = executable_path
            self.browser = await self._playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            if _is_missing_executable_error(exc):
                msg = str(exc).lower()
                if "chrome-headless-shell-mac-x64" in msg:
                    allow_system = (os.getenv("PLAYWRIGHT_ALLOW_SYSTEM_EXECUTABLE") or "").strip().lower() in {"1", "true", "yes"}
                    system_executable = (executable_path or _resolve_system_chromium_executable()) if allow_system else None
                    if not system_executable:
                        await _install_playwright_chromium(original_error=str(exc))
                        launch_kwargs = {"headless": self.headless, "args": launch_args, "env": browser_env}
                        if executable_path:
                            launch_kwargs["executable_path"] = executable_path
                        self.browser = await self._playwright.chromium.launch(**launch_kwargs)
                    else:
                        logger.warning(
                            "Headless shell x64 binary missing; retrying Bhulekh launch with system Chromium fallback (headless=%s).",
                            self.headless,
                        )
                        launch_kwargs = {
                            "headless": self.headless,
                            "args": launch_args,
                            "env": browser_env,
                            "executable_path": system_executable,
                        }
                        self.browser = await self._playwright.chromium.launch(**launch_kwargs)
                else:
                    await _install_playwright_chromium(original_error=str(exc))
                    launch_kwargs: dict[str, Any] = {
                        "headless": self.headless,
                        "args": launch_args,
                        "env": browser_env,
                    }
                    if executable_path:
                        launch_kwargs["executable_path"] = executable_path
                    self.browser = await self._playwright.chromium.launch(**launch_kwargs)
            elif self.headless and _is_gl_launch_failure(exc) and allow_headed_fallback:
                logger.warning(
                    "Bhulekh headless Chromium launch failed with GL init error; retrying headed fallback."
                )
                launch_kwargs: dict[str, Any] = {
                    "headless": False,
                    "args": launch_args,
                    "env": browser_env,
                }
                if executable_path:
                    launch_kwargs["executable_path"] = executable_path
                self.browser = await self._playwright.chromium.launch(**launch_kwargs)
            else:
                raise
        try:
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
            )
            self.page = await self.context.new_page()
        except Exception as exc:
            if self.headless and _is_gl_launch_failure(exc) and allow_headed_fallback:
                logger.warning(
                    "Bhulekh context/page creation failed under headless Chromium; relaunching headed fallback."
                )
                try:
                    await self.browser.close()
                except Exception:
                    pass
                self.browser = await self._playwright.chromium.launch(
                    headless=False,
                    args=launch_args,
                    env=browser_env,
                )
                self.context = await self.browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="en-GB",
                )
                self.page = await self.context.new_page()
            else:
                raise
        self.page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        logger.info("Playwright ready for Bhulekh.")

    async def close(self) -> None:
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        self.page = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Bhulekh browser closed.")

    async def _wait_postback_quiet(self, timeout_ms: int = 60000) -> None:
        """Wait for ASP.NET UpdatePanel overlay to finish.

        Two-phase wait:
          1. Wait up to 2 s for the UpdateProgress overlay to *appear* (the AJAX
             request may not have fired yet when we enter this function).
          2. Wait up to *timeout_ms* for the overlay to *disappear*.

        Skipping phase 1 was the root cause of a race condition where the
        function saw the overlay as already-hidden before the AJAX even started
        and returned after only the 0.4 s sleep, letting callers read
        still-stale dropdowns.
        """
        assert self.page is not None
        # Phase 1 — wait for overlay to become visible (non-fatal, short timeout).
        try:
            await self.page.wait_for_function(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    return st.display !== 'none' && st.visibility !== 'hidden';
                }""",
                arg=UPDATE_PROGRESS,
                timeout=2000,
            )
        except Exception:
            pass  # overlay may be very brief or absent on fast responses
        # Phase 2 — wait for overlay to become hidden (the postback has settled).
        try:
            await self.page.wait_for_function(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return true;
                    const st = window.getComputedStyle(el);
                    return st.display === 'none' || st.visibility === 'hidden';
                }""",
                arg=UPDATE_PROGRESS,
                timeout=timeout_ms,
            )
        except Exception:
            logger.warning("UpdateProgress wait timed out or missing; continuing.")
        await asyncio.sleep(0.4)

    async def _wait_for_dropdown_options(
        self,
        selector: str,
        min_options: int = 2,
        timeout_ms: int = 15000,
    ) -> None:
        """Block until *selector* <select> has at least *min_options* non-empty values.

        Called after each cascading dropdown selection (district→taluka,
        taluka→village) to guarantee the downstream dropdown is populated before
        we try to read it.  Times out gracefully so callers can still attempt to
        read whatever is present.
        """
        assert self.page is not None
        try:
            await self.page.wait_for_function(
                """([sel, minOpts]) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    return Array.from(el.options).filter(o => o.value).length >= minOpts;
                }""",
                arg=[selector, min_options],
                timeout=timeout_ms,
            )
            logger.info(
                "Dropdown %s populated (≥%d options) within %dms.",
                selector,
                min_options,
                timeout_ms,
            )
        except Exception:
            logger.warning(
                "Dropdown %s did not reach %d options within %dms; proceeding with current state.",
                selector,
                min_options,
                timeout_ms,
            )

    async def load_portal(self) -> None:
        assert self.page is not None
        logger.info(
            "[Bhulekh %s] Loading portal: %s", STEP_LOAD, BASE_URL
        )
        await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        await rate_limit_delay()
        await self.page.wait_for_selector(SEL_DIST, timeout=30000)
        await self._wait_postback_quiet()
        logger.info(
            "[Bhulekh %s] Page ready (district dropdown visible).", STEP_LOAD
        )

    async def get_select_options(self, selector: str) -> list[dict[str, str]]:
        """Return [{value, label}, ...] for a <select> on the current page."""
        assert self.page is not None
        return await self.page.eval_on_selector(
            selector,
            """sel => Array.from(sel.options).map(o => ({
                value: o.value,
                label: (o.textContent || '').trim()
            }))""",
        )

    async def list_district_options(self) -> list[dict[str, str]]:
        await rate_limit_delay()
        opts = await self.get_select_options(SEL_DIST)
        return [o for o in opts if o.get("value")]

    async def select_district(self, district_value: str, *, label_hint: str = "") -> None:
        assert self.page is not None
        logger.info(
            "[Bhulekh %s] Selecting district value=%s%s",
            STEP_DISTRICT,
            district_value,
            f" ({label_hint})" if label_hint else "",
        )
        await rate_limit_delay()
        await self.page.select_option(SEL_DIST, value=district_value)
        await self._wait_postback_quiet()
        await self._wait_for_dropdown_options(SEL_TALUKA)
        logger.info("[Bhulekh %s] District postback finished.", STEP_DISTRICT)

    async def list_taluka_options(self) -> list[dict[str, str]]:
        await rate_limit_delay()
        opts = await self.get_select_options(SEL_TALUKA)
        return [o for o in opts if o.get("value")]

    async def select_taluka(self, taluka_value: str, *, label_hint: str = "") -> None:
        assert self.page is not None
        logger.info(
            "[Bhulekh %s] Selecting taluka value=%s%s",
            STEP_TALUKA,
            taluka_value,
            f" ({label_hint})" if label_hint else "",
        )
        await rate_limit_delay()
        await self.page.select_option(SEL_TALUKA, value=taluka_value)
        await self._wait_postback_quiet()
        await self._wait_for_dropdown_options(SEL_VILLAGE)
        logger.info("[Bhulekh %s] Taluka postback finished.", STEP_TALUKA)

    async def list_village_options(self) -> list[dict[str, str]]:
        await rate_limit_delay()
        opts = await self.get_select_options(SEL_VILLAGE)
        return [o for o in opts if o.get("value")]

    async def select_village(self, village_value: str, *, label_hint: str = "") -> None:
        assert self.page is not None
        logger.info(
            "[Bhulekh %s] Selecting village value=%s%s",
            STEP_VILLAGE,
            village_value,
            f" ({label_hint})" if label_hint else "",
        )
        await rate_limit_delay()
        await self.page.select_option(SEL_VILLAGE, value=village_value)
        await self._wait_postback_quiet()
        logger.info("[Bhulekh %s] Village postback finished.", STEP_VILLAGE)

    async def select_survey_number_type(self, option_value: str = "2") -> None:
        """Default '2' = सर्वे नंबर (numeric survey)."""
        assert self.page is not None
        logger.info(
            "[Bhulekh %s] Selecting survey search type option value=%s (सर्वे नंबर)",
            STEP_SURVEY_TYPE,
            option_value,
        )
        await rate_limit_delay()
        await self.page.select_option(SEL_SURVEY_TYPE, value=option_value)
        await self._wait_postback_quiet()

    async def fill_survey_part1_and_search(self, part1: str) -> None:
        assert self.page is not None
        logger.info(
            "[Bhulekh %s] Survey part 1 (txtcsno)=%r → click Search",
            STEP_SURVEY_PART1,
            part1,
        )
        await rate_limit_delay()
        await self.page.fill(TXT_SURVEY_PART1, part1)
        await self.page.click(BTN_FIND_SURVEY)
        await self._wait_postback_quiet()
        await rate_limit_delay()
        opts = await self.list_survey_number_options()
        logger.info(
            "[Bhulekh %s] Survey dropdown populated: %s option(s). Sample labels: %s",
            STEP_SURVEY_PART1,
            len(opts),
            [o.get("label") for o in opts[:8]],
        )

    async def select_survey_number(self, survey_value: str, *, label_hint: str = "") -> None:
        assert self.page is not None
        logger.info(
            "[Bhulekh %s] Selecting survey number value=%s%s",
            STEP_SURVEY_NO,
            survey_value,
            f" ({label_hint})" if label_hint else "",
        )
        await rate_limit_delay()
        await self.page.select_option(SEL_SURVEY_NO, value=survey_value)
        await self._wait_postback_quiet()

    async def list_survey_number_options(self) -> list[dict[str, str]]:
        await rate_limit_delay()
        opts = await self.get_select_options(SEL_SURVEY_NO)
        return [o for o in opts if o.get("value")]

    async def solve_captcha(self) -> Optional[str]:
        import captcha_solver

        assert self.page is not None
        path = "/tmp/bhulekh_captcha.png"
        try:
            img = self.page.locator(IMG_CAPTCHA)
            await img.wait_for(state="visible", timeout=15000)
            await asyncio.sleep(0.35)
            # Inline data-URL images sometimes capture badly; write bytes from src
            src = await img.get_attribute("src")
            _debug_log(
                "H1",
                "captcha_src_seen",
                {
                    "src_type": "data_url" if (src and src.strip().lower().startswith("data:")) else "url_or_none",
                    "src_len": len(src or ""),
                },
            )
            if src and src.strip().lower().startswith("data:"):
                m = re.match(
                    r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$",
                    src.strip(),
                    flags=re.DOTALL,
                )
                if m:
                    try:
                        raw = base64.b64decode(m.group("data").replace("\n", ""))
                        Path(path).write_bytes(raw)
                        _debug_log("H1", "captcha_data_url_written", {"byte_len": len(raw)})
                    except Exception:
                        await img.screenshot(path=path)
                        _debug_log("H1", "captcha_data_url_decode_failed_screenshot", {})
                else:
                    await img.screenshot(path=path)
                    _debug_log("H1", "captcha_data_url_regex_failed_screenshot", {})
            else:
                await img.screenshot(path=path)
                _debug_log("H1", "captcha_element_screenshot_used", {})

            text = captcha_solver.solve(path)
            logger.info("Bhulekh captcha OCR: %r", text)
            _debug_log("H2", "captcha_ocr_result", {"text_len": len(text or "")})
            try:
                os.remove(path)
            except OSError:
                pass
            return text if text else None
        except Exception as e:
            logger.error("Captcha solve failed: %s", e)
            return None

    async def _refresh_captcha(self) -> None:
        assert self.page is not None
        await self._dismiss_result_overlay()
        try:
            btn = self.page.locator(BTN_REFRESH_CAPTCHA)
            if await btn.count() > 0:
                await btn.first.click()
                await asyncio.sleep(1.2)
                logger.info("Captcha refreshed.")
            else:
                logger.warning("Captcha refresh button not found.")
        except Exception:
            logger.warning("Could not click captcha refresh.")

    async def _dismiss_result_overlay(self) -> None:
        """
        Close/hide report popup overlays that can block submit/captcha controls.
        """
        assert self.page is not None
        try:
            await self.page.evaluate(
                """
                () => {
                    const ids = [
                        'ContentPlaceHolder1_showPopUp',
                        'ContentPlaceHolder1_show8a',
                        'ContentPlaceHolder1_showreport',
                    ];
                    for (const id of ids) {
                        const el = document.getElementById(id);
                        if (!el) continue;
                        el.style.display = 'none';
                        el.classList.remove('showPC', 'show8a', 'report');
                    }
                    const blur = document.querySelector('.blur-overlay, .overlay');
                    if (blur) blur.remove();
                }
                """
            )
        except Exception:
            pass
        # Try clicking known close controls if present
        for sel in (
            "#btnClosePopup",
            "#btnClose8a",
            "#btnCloseReport",
            "#ContentPlaceHolder1_btnClosePopup",
        ):
            try:
                loc = self.page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=1000)
            except Exception:
                pass

    async def _captcha_failed(self) -> bool:
        assert self.page is not None
        try:
            err = self.page.locator("#ContentPlaceHolder1_lblerrortext, #ContentPlaceHolder1_lblerror12")
            txt = await err.first.text_content()
            if txt and txt.strip():
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _looks_like_unchanged_form(before_html: str, after_html: str) -> bool:
        """
        Bhulekh often returns the same form markup when captcha is wrong,
        without populating explicit error labels.
        """
        if not after_html:
            return True
        # Main form markers that should usually disappear/shift when a report view appears
        form_markers = (
            "Do You Know Your 11 Digit Property UID Number?",
            "ContentPlaceHolder1_btnmainsubmit",
            "ContentPlaceHolder1_txtcaptcha",
            "ContentPlaceHolder1_ddlMainDist",
        )
        if not all(m in after_html for m in form_markers):
            return False
        # Compare prefix where the bulk layout lives
        a = (before_html or "")[:50000]
        b = after_html[:50000]
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        return ratio >= 0.85

    async def _submit_likely_succeeded(self) -> bool:
        """
        Detect 7/12 result UI. On a correct captcha the page may still look like the same
        form (captcha image refreshes); rely on popup / report / PC image instead of HTML diff.
        """
        assert self.page is not None
        try:
            return await self.page.evaluate(
                """() => {
                    function visible(el) {
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden') return false;
                        const r = el.getBoundingClientRect();
                        return r.width >= 2 && r.height >= 2;
                    }
                    const panelIds = [
                        'ContentPlaceHolder1_showPopUp',
                        'ContentPlaceHolder1_show8a',
                        'ContentPlaceHolder1_showreport',
                    ];
                    for (const id of panelIds) {
                        const el = document.getElementById(id);
                        if (!el) continue;
                        if (visible(el)) return true;
                        if (el.classList.contains('showPC') || el.classList.contains('show8a')
                            || el.classList.contains('report')) {
                            const st = window.getComputedStyle(el);
                            if (st.display !== 'none' && st.visibility !== 'hidden') return true;
                        }
                    }
                    const imgPc = document.getElementById('ContentPlaceHolder1_ImgPC');
                    if (imgPc && imgPc.naturalWidth > 16 && visible(imgPc)) return true;
                    const lblpc = document.getElementById('ContentPlaceHolder1_lblpc');
                    if (lblpc && (lblpc.textContent || '').trim().length > 0) return true;
                    return false;
                }"""
            )
        except Exception:
            return False

    async def _wait_for_submit_outcome(self, timeout_s: float = 12.0) -> None:
        """Poll until result UI appears, validation errors show, or timeout."""
        assert self.page is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await self._submit_likely_succeeded():
                logger.info("Submit outcome signal: result UI became visible.")
                return
            if await self._captcha_failed():
                logger.info("Submit outcome signal: captcha/validation error became visible.")
                return
            await asyncio.sleep(0.35)
        logger.info("Submit outcome wait timed out after %.1fs; proceeding with HTML heuristics.", timeout_s)

    async def submit_with_captcha(self, params: BhulekhSearchParams) -> str:
        """
        Fill mobile, English, captcha, submit. Returns final page HTML.
        Retries captcha on failure signals.
        """
        assert self.page is not None
        t0 = time.monotonic()

        logger.info(
            "[Bhulekh %s] Mobile=%s language=%s (English=en_in)",
            STEP_CAPTCHA_SUBMIT,
            params.mobile,
            params.language_value,
        )
        await self.page.fill(TXT_MOBILE, params.mobile)
        await self.page.select_option(SEL_LANG, value=params.language_value)
        await self._wait_postback_quiet()

        for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
            logger.info(
                "[Bhulekh %s] Captcha attempt %s/%s",
                STEP_CAPTCHA_SUBMIT,
                attempt,
                MAX_CAPTCHA_RETRIES,
            )
            await rate_limit_delay()
            captcha_text = await self.solve_captcha()
            if not captcha_text:
                _debug_log("H3", "captcha_text_empty_retry", {"attempt": attempt})
                await self._refresh_captcha()
                continue
            await self.page.fill(TXT_CAPTCHA, captcha_text)
            await self._dismiss_result_overlay()
            before_submit_html = await self.page.content()
            await self.page.click(BTN_SUBMIT)
            await self._wait_postback_quiet()
            await self._wait_for_submit_outcome()
            after_submit_html = await self.page.content()

            captcha_failed = await self._captcha_failed()
            submit_succeeded = await self._submit_likely_succeeded()
            unchanged = self._looks_like_unchanged_form(before_submit_html, after_submit_html)
            _debug_log(
                "H4",
                "submit_outcome_flags",
                {
                    "attempt": attempt,
                    "captcha_text_len": len(captcha_text or ""),
                    "captcha_failed": captcha_failed,
                    "submit_succeeded": submit_succeeded,
                    "unchanged_form": unchanged,
                    "before_len": len(before_submit_html or ""),
                    "after_len": len(after_submit_html or ""),
                },
            )

            if captcha_failed:
                logger.warning("Possible captcha/validation error; retrying.")
                await self._refresh_captcha()
                continue

            if submit_succeeded:
                logger.info(
                    "[Bhulekh %s] Submit completed (result UI visible; captcha may have refreshed) "
                    "in %.1fs (HTML length=%s chars).",
                    STEP_DONE,
                    time.monotonic() - t0,
                    len(after_submit_html),
                )
                return after_submit_html

            if unchanged:
                logger.warning(
                    "Submit returned unchanged form (likely bad captcha or no result render); retrying."
                )
                await self._refresh_captcha()
                continue

            logger.info(
                "[Bhulekh %s] Submit completed in %.1fs (HTML length=%s chars).",
                STEP_DONE,
                time.monotonic() - t0,
                len(after_submit_html),
            )
            return after_submit_html

        raise RuntimeError(f"Captcha not accepted after {MAX_CAPTCHA_RETRIES} attempts.")

    async def run_search(self, params: BhulekhSearchParams) -> str:
        """Full happy path: dropdowns → survey chain → submit → HTML document."""
        logger.info(
            "Bhulekh run_search start (codes): district=%s taluka=%s village=%s "
            "part1=%s survey_no_value=%s",
            params.district_value,
            params.taluka_value,
            params.village_value,
            params.survey_part1,
            params.survey_number_value,
        )
        await self.load_portal()

        await self.select_district(params.district_value)
        await self.select_taluka(params.taluka_value)
        await self.select_village(params.village_value)
        await self.select_survey_number_type(params.survey_type_option_value)
        await self.fill_survey_part1_and_search(params.survey_part1)
        await self.select_survey_number(params.survey_number_value)

        html = await self.submit_with_captcha(params)
        return html

    async def run_search_with_labels(
        self,
        district_label: str,
        taluka_label: str,
        village_label: str,
        survey_part1: str,
        survey_option_label: str,
        *,
        mobile: str = "9999999999",
        language_value: str = "en_in",
        survey_type_option_value: str = "2",
    ) -> str:
        """
        Resolve dropdown codes from partial English/Marathi labels, then full flow.

        ``survey_option_label`` matches text in the survey dropdown (e.g. ``1530/3``).
        """
        logger.info(
            "Bhulekh run_search_with_labels: district=%r taluka=%r village=%r "
            "part1=%r survey_option_label=%r",
            district_label,
            taluka_label,
            village_label,
            survey_part1,
            survey_option_label,
        )

        await self.load_portal()

        districts = await self.list_district_options()
        dv = find_option_value_by_label(districts, district_label)
        if not dv:
            labels = [d.get("label") for d in districts[:20]]
            raise ValueError(
                f"District not found for {district_label!r}. Sample labels: {labels}..."
            )
        lab_d = next(
            (d.get("label") for d in districts if d.get("value") == dv), dv
        )
        await self.select_district(dv, label_hint=str(lab_d))

        talukas = await self.list_taluka_options()
        tv = find_option_value_by_label(talukas, taluka_label)
        if not tv:
            t_labels = [t.get("label") for t in talukas[:20]]
            raise ValueError(
                f"Taluka not found for {taluka_label!r} under selected district. "
                f"Available ({len(talukas)}): {t_labels}..."
            )
        lab_t = next(
            (t.get("label") for t in talukas if t.get("value") == tv), tv
        )
        await self.select_taluka(tv, label_hint=str(lab_t))

        villages = await self.list_village_options()
        vv = find_option_value_by_label(villages, village_label)
        if not vv:
            v_labels = [v.get("label") for v in villages[:30]]
            raise ValueError(
                f"Village not found for {village_label!r} under selected taluka. "
                f"Available ({len(villages)}): {v_labels}..."
            )
        lab_v = next(
            (v.get("label") for v in villages if v.get("value") == vv), vv
        )
        await self.select_village(vv, label_hint=str(lab_v))

        await self.select_survey_number_type(survey_type_option_value)
        await self.fill_survey_part1_and_search(survey_part1)

        surveys = await self.list_survey_number_options()
        sv = find_option_value_by_label(surveys, survey_option_label)
        if not sv:
            all_labels = [s.get("label") for s in surveys]
            raise ValueError(
                f"Survey option not matching {survey_option_label!r}. "
                f"Available ({len(all_labels)}): {all_labels}"
            )
        lab_s = next(
            (s.get("label") for s in surveys if s.get("value") == sv), sv
        )
        await self.select_survey_number(sv, label_hint=str(lab_s))

        merged = BhulekhSearchParams(
            district_value=dv,
            taluka_value=tv,
            village_value=vv,
            survey_part1=survey_part1,
            survey_number_value=sv,
            mobile=mobile,
            survey_type_option_value=survey_type_option_value,
            language_value=language_value,
        )
        html = await self.submit_with_captcha(merged)
        return html

    async def _pick_visible_result_panel_selector(self) -> Optional[str]:
        """Which ASP.NET panel currently holds the post-submit land record (if any)."""
        assert self.page is not None
        for sel in RESULT_PANEL_SELECTORS:
            loc = self.page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                if await loc.is_visible():
                    logger.info("Detected visible result panel: %s", sel)
                    return sel
            except Exception:
                continue
        logger.info("No visible result panel detected among: %s", ", ".join(RESULT_PANEL_SELECTORS))
        return None

    async def _apply_land_record_print_css(self, panel_selector: str) -> None:
        """
        Chromium's page.pdf() prints the whole viewport by default. Inject print CSS so only
        the record popup/panel is visible — same technique as browser Print > selection, but
        automated via @media print.
        """
        assert self.page is not None
        # panel_selector is a fixed #id from RESULT_PANEL_SELECTORS (safe to embed).
        css = f"""
@media print {{
  @page {{ margin: 0.4cm; }}
  html, body {{
    margin: 0 !important;
    padding: 0 !important;
    background: #fff !important;
  }}
  .uwy, .userway_p1, [class^="userway"] {{
    display: none !important;
  }}
  body * {{ visibility: hidden !important; }}
  {panel_selector} {{
    visibility: visible !important;
    position: relative !important;
    left: 0 !important;
    top: 0 !important;
    width: 100% !important;
    max-width: 100% !important;
    height: auto !important;
    overflow: visible !important;
    box-shadow: none !important;
    background: #fff !important;
  }}
  {panel_selector} * {{ visibility: visible !important; }}
}}
"""
        await self.page.add_style_tag(content=css)

    async def save_verification_pdf(self, output_path: str | Path) -> Path:
        """
        Write a print-style PDF for local verification.

        The live site renders the 7/12 in a modal/panel on the same page. By default
        ``page.pdf()`` would include the map, form sidebar, and headers; we inject
        ``@media print`` rules to print only the visible result panel when possible.

        If the passed path has no ``.pdf`` suffix, ``.pdf`` is applied.
        """
        assert self.page is not None
        out = Path(output_path)
        pdf_path = out if out.suffix.lower() == ".pdf" else out.with_suffix(".pdf")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        panel = await self._pick_visible_result_panel_selector()
        if panel:
            try:
                await self.page.locator(panel).first.scroll_into_view_if_needed()
            except Exception:
                pass
            await self._apply_land_record_print_css(panel)
            logger.info("Verification PDF: printing isolated result panel %s", panel)
        else:
            logger.warning(
                "Verification PDF: no visible result panel (%s); full-page print may include site chrome.",
                ", ".join(RESULT_PANEL_SELECTORS),
            )

        await self.page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        logger.info("Verification PDF written: %s", pdf_path)
        return pdf_path.resolve()

    async def save_submit_artifacts(
        self,
        html_output_path: str | Path,
        *,
        include_pdf: bool = False,
        max_downloads: int = 10,
    ) -> list[Path]:
        """
        Save post-submit artifacts next to output HTML:
        - full-page screenshot
        - optional PDF print
        - embedded/linked document images and PDFs found in result HTML
        """
        assert self.page is not None
        assert self.context is not None

        out_html = Path(html_output_path)
        assets_dir = out_html.with_suffix("").with_name(f"{out_html.stem}_assets")
        assets_dir.mkdir(parents=True, exist_ok=True)

        saved: list[Path] = []

        shot_path = assets_dir / "submitted_page.png"
        try:
            await self.page.screenshot(path=str(shot_path), full_page=True)
            saved.append(shot_path.resolve())
        except Exception as e:
            logger.warning("Could not save submit screenshot: %s", e)

        if include_pdf:
            pdf_path = assets_dir / "submitted_page.pdf"
            try:
                panel = await self._pick_visible_result_panel_selector()
                if panel:
                    await self._apply_land_record_print_css(panel)
                    logger.info("Submit artifact PDF: printing isolated panel %s", panel)
                else:
                    logger.warning("Submit artifact PDF: result panel not found; printing full page.")
                await self.page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                )
                saved.append(pdf_path.resolve())
            except Exception as e:
                logger.warning("Could not save submit PDF print: %s", e)

        html = await self.page.content()
        candidates = extract_document_resource_urls(html, base_url=BASE_URL)
        if candidates:
            logger.info(
                "Found %s candidate document/image resource(s) in response.",
                len(candidates),
            )
        for i, url in enumerate(candidates[:max_downloads], start=1):
            path = await self._download_resource(url, assets_dir, i)
            if path:
                saved.append(path.resolve())

        return saved

    async def _download_resource(
        self,
        resource_url: str,
        assets_dir: Path,
        index: int,
    ) -> Optional[Path]:
        if resource_url.startswith("data:"):
            return _save_data_url(resource_url, assets_dir, index)

        try:
            resp = await self.context.request.get(resource_url, timeout=30000)
            if not resp.ok:
                return None
            ctype = (resp.headers.get("content-type") or "").lower()
            if not (
                ctype.startswith("image/")
                or "pdf" in ctype
                or resource_url.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"))
            ):
                return None
            data = await resp.body()
            ext = _extension_from_content_type_or_url(ctype, resource_url)
            stem = _safe_name_from_url(resource_url) or f"resource_{index}"
            path = assets_dir / f"{stem}{ext}"
            path = _dedupe_path(path)
            path.write_bytes(data)
            return path
        except Exception:
            return None

    async def collect_dropdown_snapshot(
        self,
        district_value: str,
        taluka_value: str,
    ) -> dict[str, Any]:
        """
        Load portal, capture districts, drill into district → taluka → villages.

        Survey numbers depend on part-1 search — use ``list_survey_number_options``
        after ``fill_survey_part1_and_search`` when needed.
        """
        await self.load_portal()
        districts = await self.list_district_options()
        await self.select_district(district_value)
        talukas = await self.list_taluka_options()
        await self.select_taluka(taluka_value)
        villages = await self.list_village_options()
        return {
            "districts": districts,
            "talukas": talukas,
            "villages": villages,
        }


def save_document_html(html: str, path: str | Path) -> Path:
    """Write raw response HTML to disk."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    return p.resolve()


def extract_document_resource_urls(html: str, base_url: str) -> list[str]:
    """
    Collect likely post-submit document resources from HTML.
    Prioritizes report/document links while skipping known decorative assets.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()

    selectors = [
        "img[src]",
        "a[href]",
        "iframe[src]",
        "object[data]",
        "embed[src]",
    ]
    attrs = {"img[src]": "src", "a[href]": "href", "iframe[src]": "src", "object[data]": "data", "embed[src]": "src"}

    for sel in selectors:
        attr = attrs[sel]
        for node in soup.select(sel):
            raw = (node.get(attr) or "").strip()
            if not raw:
                continue
            if raw.startswith("javascript:") or raw.startswith("#"):
                continue
            full = raw if raw.startswith("data:") else urljoin(base_url, raw)
            norm = full.lower()
            if any(x in norm for x in ("dept-logo", "div1_map", "captcha", "userway", "spin_wh")):
                continue
            # keep clearly document-like targets and data urls
            if full.startswith("data:") or any(
                k in norm for k in ("report", "satbara", "7/12", "download", "pdf", "image", "property")
            ) or norm.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf")):
                if full not in seen:
                    seen.add(full)
                    urls.append(full)
    return urls


def _safe_name_from_url(url: str) -> str:
    path = urlparse(url).path
    name = Path(path).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name or "resource"


def _extension_from_content_type_or_url(content_type: str, url: str) -> str:
    if "pdf" in content_type:
        return ".pdf"
    if content_type.startswith("image/"):
        sub = content_type.split("/", 1)[1].split(";", 1)[0].strip()
        if sub == "jpeg":
            return ".jpg"
        if sub:
            return f".{sub}"
    ext = Path(urlparse(url).path).suffix.lower()
    return ext or ".bin"


def _save_data_url(data_url: str, assets_dir: Path, index: int) -> Optional[Path]:
    m = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", data_url, flags=re.I | re.S)
    if not m:
        return None
    mime = (m.group(1) or "").lower()
    is_b64 = bool(m.group(2))
    payload = m.group(3) or ""
    try:
        data = base64.b64decode(payload) if is_b64 else payload.encode("utf-8")
    except Exception:
        return None
    ext = mimetypes.guess_extension(mime) if mime else None
    if ext == ".jpe":
        ext = ".jpg"
    ext = ext or ".bin"
    path = _dedupe_path(assets_dir / f"embedded_{index}{ext}")
    path.write_bytes(data)
    return path


def _dedupe_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    i = 2
    while True:
        p = parent / f"{stem}_{i}{suffix}"
        if not p.exists():
            return p
        i += 1


# English CLI hints → common Marathi substrings on NewBhulekh dropdowns (extend as needed)
_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "pune": ("पुणे", "pune"),
    "satara": ("सातारा", "satara"),
    "haveli": ("हवेली", "haveli"),
    "baner": ("बाणेर", "baner", "bner", "baaner"),
    "mulshi": ("मुळशी", "मुळ्शी", "mulshi", "mulashi"),
    "wakad": ("वाकड", "wakad"),
    "uruli": ("उरुळी", "उरली", "uruli"),
    "uruli kanchan": ("उरुळी कांचन", "uruli kanchan"),
    "uruli devachi": ("उरुळी देवाची", "उरुळीदेवाची", "uruli devachi"),
    "waghol": ("वाघोली", "वाघोळी", "waghol", "wagoli"),
    "wagholi": ("वाघोली", "वाघोळी", "waghol", "wagoli"),
    "karve nagar": ("कर्वेनगर", "म .कर्वेनगर", "karvenagar", "karve nagar"),
    "karvenagar": ("कर्वेनगर", "म .कर्वेनगर", "karve nagar"),
}

_LATIN_TO_DEV_SUFFIX = {
    "a": "अ",
    "b": "ब",
    "c": "क",
    "d": "ड",
}
_DEV_TO_LATIN_SUFFIX = {v: k for k, v in _LATIN_TO_DEV_SUFFIX.items()}
_DEV_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_LABEL_ALIAS_LOOKUP: dict[str, tuple[str, ...]] = {}


def _build_label_alias_lookup() -> dict[str, tuple[str, ...]]:
    """
    Build reverse alias lookup so Marathi inputs (e.g. मुळ्शी) also expand.
    """
    clusters: list[set[str]] = []
    for key, members in _LABEL_ALIASES.items():
        cluster = {key.strip().lower()}
        cluster.update(m.strip().lower() for m in members if (m or "").strip())
        clusters.append(cluster)

    lookup: dict[str, tuple[str, ...]] = {}
    for cluster in clusters:
        expanded = tuple(sorted(cluster))
        for token in cluster:
            lookup[token] = expanded
    return lookup


_LABEL_ALIAS_LOOKUP = _build_label_alias_lookup()


def _sanitize_label_input(value: str) -> str:
    """
    Clean user-provided label text for resilient matching.

    Handles common copy/paste and encoding artifacts:
      - BOM/zero-width chars
      - Unicode replacement char (U+FFFD)
      - literal '?' introduced by lossy decode
    """
    txt = unicodedata.normalize("NFKC", value or "")
    txt = txt.replace("\ufeff", "")
    txt = txt.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    txt = txt.replace("\ufffd", "").replace("?", "")
    return txt.strip()


def _canonical_label_text(value: str) -> str:
    """
    Normalize script/format variants commonly seen in Bhulekh dropdown labels.
    Examples:
      - २०४/६अ -> 204/6अ
      - 204 / 6 A -> 204/6a
    """
    txt = _sanitize_label_input(value)
    txt = txt.translate(_DEV_TO_ASCII_DIGITS)
    txt = txt.strip().lower()
    txt = re.sub(r"\s+", "", txt)
    return txt


def _match_needles_against_label(label: str, needles: list[str]) -> bool:
    lab = (label or "").strip()
    lab_lower = lab.lower()
    lab_canonical = _canonical_label_text(lab)
    for needle in needles:
        n = needle.strip().lower()
        if not n:
            continue
        n_canonical = _canonical_label_text(n)
        if n in lab_lower:
            return True
        if n_canonical and n_canonical in lab_canonical:
            return True
        # Labels often look like "पुणे(Pune)" — match English inside parentheses
        for m in re.finditer(r"\(([^)]+)\)", lab):
            inner = m.group(1).strip().lower()
            if n == inner or n in inner:
                return True
    return False


def _expand_label_needles(label_substring: str) -> list[str]:
    cleaned = _sanitize_label_input(label_substring)
    base = cleaned.lower()
    needles: list[str] = [base] if base else []
    extra = _LABEL_ALIAS_LOOKUP.get(base)
    if extra:
        needles.extend(extra)
    # Handle survey label suffix equivalence (e.g. 204/6A <-> 204/6अ).
    m_latin = re.fullmatch(r"(.+?)([a-z])", base, flags=re.IGNORECASE)
    if m_latin:
        stem, suffix = m_latin.group(1), m_latin.group(2).lower()
        dev = _LATIN_TO_DEV_SUFFIX.get(suffix)
        if dev:
            needles.append(f"{stem}{dev}".lower())
    m_dev = re.fullmatch(r"(.+?)([\u0900-\u097f])", base)
    if m_dev:
        stem, suffix = m_dev.group(1), m_dev.group(2)
        latin = _DEV_TO_LATIN_SUFFIX.get(suffix)
        if latin:
            needles.append(f"{stem}{latin}".lower())
    return needles


def find_option_value_by_label(options: list[dict[str, str]], label_substring: str) -> Optional[str]:
    """Match district/taluka/village/survey option by partial English/Marathi label."""
    needles = _expand_label_needles(label_substring)
    if not needles:
        return None
    for o in options:
        lab = o.get("label") or ""
        if _match_needles_against_label(lab, needles):
            return o.get("value")
    return None


def normalize_indian_mobile(m: str) -> bool:
    return bool(re.fullmatch(r"[6-9][0-9]{9}", (m or "").strip()))
