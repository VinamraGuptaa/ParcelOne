"""
FreeSearch IGR Maharashtra scraper (Rest of Maharashtra tab).

This module is intentionally structured for network-first scraping:
- bootstrap browser session
- close popups / switch to Rest of Maharashtra tab
- solve captcha via RapidOCR
- submit search and parse tabular results
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from playwright_launch_args import chromium_launch_args

logger = logging.getLogger(__name__)

IGR_URL = "https://freesearchigrservice.maharashtra.gov.in/"
MAX_CAPTCHA_RETRIES = 5

# Navigation timeout for page.goto / page.reload calls.  The IGR govt portal
# can be genuinely slow; 90 s is a pragmatic baseline.  Override via env var.
IGR_GOTO_TIMEOUT_MS = int(os.getenv("IGR_GOTO_TIMEOUT_MS", "90000"))
# How many times to retry a timed-out navigation *within* setup_driver before
# propagating the error to the outer _run_with_retries loop.
IGR_GOTO_RETRIES = int(os.getenv("IGR_GOTO_RETRIES", "3"))
# How long to poll for RegistrationGrid / zero-results text after Search submit.
IGR_RESULTS_WAIT_SECONDS = float(os.getenv("IGR_RESULTS_WAIT_SECONDS", "45"))
# Optional early skip when the page stays indeterminate. 0 = disabled (wait full results_wait).
# Do not set this too low — IGR often needs 25–40s after a correct captcha before the grid appears.
IGR_PENDING_STALL_SECONDS = float(os.getenv("IGR_PENDING_STALL_SECONDS", "0"))
# After this many consecutive empty OCR reads, reload the portal and refill the form.
IGR_EMPTY_OCR_RECOVERY_THRESHOLD = int(os.getenv("IGR_EMPTY_OCR_RECOVERY_THRESHOLD", "3"))

_IGR_ZERO_RESULTS_PHRASE = "आढळून आलेली नाही"
_IGR_ZERO_RESULT_EN_MARKERS = (
    "record not found",
    "no record found",
    "no records found",
    "no data found",
    "details not found",
    "data not found",
)

SEL_YEAR = "#ddlFromYear1"
SEL_DISTRICT = "#ddlDistrict1"
SEL_TALUKA = "#ddltahsil"
SEL_VILLAGE = "#ddlvillage"
TXT_SURVEY = "#txtAttributeValue1"
IMG_CAPTCHA = "#imgCaptcha_new"
TXT_CAPTCHA = "#txtImg1"
BTN_REST_OF_MH = "#btnOtherdistrictSearch"
BTN_SEARCH = "#btnSearch_RestMaha"
BTN_CANCEL = "#btnCancel_RestMaha"

_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "pune": ("पुणे", "pune"),
    "satara": ("सातारा", "satara"),
    "haveli": ("हवेली", "haveli"),
    "baner": ("बाणेर", "baner", "bner", "baaner"),
    "mulshi": ("मुळशी", "मुळ्शी", "mulshi", "mulashi"),
    "wakad": ("वाकड", "wakad"),
    "uruli": ("उरुळी", "उरळी", "उरली", "uruli"),
    "uruli kanchan": ("उरुळी कांचन", "उरळी कांचन", "uruli kanchan"),
    "uruli devachi": ("उरुळी देवाची", "उरळी देवाची", "उरुळीदेवाची", "uruli devachi"),
    "waghol": ("वाघोली", "वाघोळी", "waghol", "wagoli"),
    "wagholi": ("वाघोली", "वाघोळी", "waghol", "wagoli"),
    "darawali": ("दारवली", "daravali", "darawali", "dara vali"),
    "daravali": ("दारवली", "darawali", "dara vali"),
    "karve nagar": ("कर्वेनगर", "म .कर्वेनगर", "karvenagar", "karve nagar"),
    "karvenagar": ("कर्वेनगर", "म .कर्वेनगर", "karve nagar"),
    "shirur": ("शिरुर", "shirur", "shirur"),
    "talegaon dhamdhere": ("तळेगांव ढमढेरे", "talegaon dhamdhere", "talegaon dhamdhere"),
    "talegaon dhamdere": ("तळेगांव ढमढेरे", "talegaon dhamdhere"),
}


def _sanitize_label_input(value: str) -> str:
    """
    Normalize label text and remove common Unicode/encoding artifacts.
    """
    txt = unicodedata.normalize("NFKC", value or "")
    txt = txt.replace("\ufeff", "")
    txt = txt.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    txt = txt.replace("\ufffd", "").replace("?", "")
    return txt.strip()


class IGRFreeSearchScraper:
    def __init__(self, headless: bool = True, shared_browser: Optional[Browser] = None):
        self.headless = headless
        # When set, this scraper borrows an already-running browser process.
        # setup_driver() will skip launching a new process and just open a new
        # BrowserContext on it.  close() will not shut down the browser.
        self._shared_browser = shared_browser
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def setup_driver(self) -> None:
        # ── Shared-browser fast path ──────────────────────────────────────────
        # When a browser process is supplied externally (e.g. a second parallel
        # context sharing the primary's process), skip playwright startup and
        # browser launch entirely — just open a fresh isolated BrowserContext.
        if self._shared_browser is not None:
            self.browser = self._shared_browser
            try:
                self.context = await self.browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                self.page = await self.context.new_page()
            except Exception:
                self.browser = None
                raise
            self.page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
            await self._navigate_to_portal()
            await asyncio.sleep(1.0)
            await self._close_startup_popup()
            await self._switch_to_rest_of_maharashtra_tab()
            logger.info("IGR scraper ready (shared browser, new context).")
            return
        # ── Full launch path (owns its own browser process) ───────────────────
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
            launch_kwargs: dict[str, object] = {
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
                            "Headless shell x64 binary missing; retrying IGR launch with system Chromium fallback (headless=%s).",
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
                    launch_kwargs: dict[str, object] = {
                        "headless": self.headless,
                        "args": launch_args,
                        "env": browser_env,
                    }
                    if executable_path:
                        launch_kwargs["executable_path"] = executable_path
                    self.browser = await self._playwright.chromium.launch(**launch_kwargs)
            elif self.headless and _is_gl_launch_failure(exc) and allow_headed_fallback:
                logger.warning(
                    "IGR headless Chromium launch failed with GL init error; retrying headed fallback."
                )
                launch_kwargs: dict[str, object] = {
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
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            self.page = await self.context.new_page()
        except Exception as exc:
            if self.headless and _is_gl_launch_failure(exc) and allow_headed_fallback:
                logger.warning(
                    "IGR context/page creation failed under headless Chromium; relaunching headed fallback."
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
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                self.page = await self.context.new_page()
            else:
                raise
        self.page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
        await self._navigate_to_portal()
        await asyncio.sleep(1.0)
        await self._close_startup_popup()
        await self._switch_to_rest_of_maharashtra_tab()
        logger.info("IGR scraper ready.")

    async def _navigate_to_portal(self) -> None:
        """Navigate to IGR_URL with per-attempt retries and exponential backoff.

        This keeps the retry logic *inside* setup_driver so a slow network
        response does not cause the outer _run_with_retries loop to tear down
        and recreate the entire browser session (which is expensive and unhelpful
        when the browser itself is healthy — only the network is slow).
        """
        assert self.page is not None
        last_exc: Exception | None = None
        for attempt in range(1, IGR_GOTO_RETRIES + 1):
            try:
                logger.info(
                    "IGR portal navigation attempt %s/%s (timeout=%dms).",
                    attempt,
                    IGR_GOTO_RETRIES,
                    IGR_GOTO_TIMEOUT_MS,
                )
                await self.page.goto(
                    IGR_URL,
                    wait_until="domcontentloaded",
                    timeout=IGR_GOTO_TIMEOUT_MS,
                )
                logger.info("IGR portal navigation succeeded on attempt %s.", attempt)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "IGR portal navigation attempt %s/%s failed: %s",
                    attempt,
                    IGR_GOTO_RETRIES,
                    exc,
                )
                if attempt < IGR_GOTO_RETRIES:
                    backoff = 5.0 * attempt  # 5 s, 10 s, …
                    logger.info(
                        "IGR portal navigation: retrying in %.0fs.", backoff
                    )
                    await asyncio.sleep(backoff)
        raise RuntimeError(
            f"IGR portal navigation failed after {IGR_GOTO_RETRIES} attempts: {last_exc}"
        ) from last_exc

    async def close(self) -> None:
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        self.page = None
        if self._shared_browser is not None:
            # Browser is externally owned — only drop our reference, do not close it.
            self.browser = None
        else:
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
        logger.info("IGR scraper closed.")

    async def _close_popups_best_effort(self) -> None:
        assert self.page is not None
        selectors = (
            "button.close",
            "button[aria-label='Close']",
            ".modal .btn-close",
            ".swal2-close",
            ".swal2-confirm",
        )
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=1200)
                    await asyncio.sleep(0.2)
            except Exception:
                continue

    async def _close_startup_popup(self) -> None:
        """
        Close initial landing popup before interacting with tabs.
        """
        assert self.page is not None
        # Try visible modal close buttons first.
        modal_close_selectors = (
            "button:has-text('Close')",
            "text=Close",
            ".modal.show button.close",
            ".modal.show .btn-close",
            ".modal.show [aria-label='Close']",
            ".swal2-container .swal2-close",
            ".swal2-container .swal2-confirm",
        )
        for sel in modal_close_selectors:
            try:
                loc = self.page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=2500)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                continue

        # Some pages render a generic "Close" button outside standard modal wrappers.
        try:
            close_btn = self.page.locator("button:has-text('Close')").first
            if await close_btn.count() > 0:
                await close_btn.click(timeout=2500)
                await asyncio.sleep(0.5)
        except Exception:
            pass

        # Fallback: hide known blocking overlays via JS if still present.
        try:
            await self.page.evaluate(
                """() => {
                    const selectors = [
                      '.modal.show',
                      '.modal-backdrop',
                      '.swal2-container',
                      '.popup',
                      '.overlay',
                    ];
                    for (const sel of selectors) {
                      for (const el of document.querySelectorAll(sel)) {
                        el.remove();
                      }
                    }
                    document.body.classList.remove('modal-open');
                    document.body.style.overflow = 'auto';
                }"""
            )
        except Exception:
            pass
        await asyncio.sleep(0.3)

    async def _switch_to_rest_of_maharashtra_tab(self) -> None:
        assert self.page is not None
        try:
            btn = self.page.locator(BTN_REST_OF_MH).first
            if await btn.count() > 0:
                await btn.click(timeout=3000)
                await asyncio.sleep(0.8)
                logger.info("Selected Rest of Maharashtra tab via %s.", BTN_REST_OF_MH)
                return
        except Exception:
            pass
        tab_selectors = (
            "a:has-text('Rest of Maharashtra')",
            "button:has-text('Rest of Maharashtra')",
            "[role='tab']:has-text('Rest of Maharashtra')",
            "text=Rest of Maharashtra",
            "text=Rest Of Maharashtra",
            "text=rest of maharashtra",
        )
        for sel in tab_selectors:
            try:
                loc = self.page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=2500)
                    await asyncio.sleep(0.6)
                    logger.info("Selected Rest of Maharashtra tab via selector: %s", sel)
                    return
            except Exception:
                continue
        # JS text-match fallback.
        try:
            clicked = await self.page.evaluate(
                """() => {
                    const targets = Array.from(document.querySelectorAll('a,button,[role="tab"],li'));
                    const isMatch = (t) => (t || '').toLowerCase().includes('rest of maharashtra');
                    for (const el of targets) {
                        const text = (el.textContent || '').trim();
                        if (!isMatch(text)) continue;
                        el.click();
                        return true;
                    }
                    return false;
                }"""
            )
            if clicked:
                await asyncio.sleep(0.6)
                logger.info("Selected Rest of Maharashtra tab via JS fallback.")
                return
        except Exception:
            pass
        logger.warning("Could not confidently click Rest of Maharashtra tab; proceeding.")

    async def _solve_captcha(self) -> str:
        import captcha_solver

        assert self.page is not None
        candidate_selectors = (IMG_CAPTCHA, "img[id*='captcha' i]", "img[src*='captcha' i]")
        captcha_path = "/tmp/igr_captcha.png"
        for sel in candidate_selectors:
            try:
                img = self.page.locator(sel).first
                if await img.count() == 0:
                    continue
                await img.wait_for(state="visible", timeout=2500)
                await img.screenshot(path=captcha_path)
                solved = captcha_solver.solve(captcha_path, mode="rapidocr_only")
                solved = self._normalize_captcha_text(solved or "")
                try:
                    os.remove(captcha_path)
                except OSError:
                    pass
                if solved:
                    logger.info("IGR captcha solved via %s (len=%s).", sel, len(solved))
                else:
                    logger.info("IGR captcha OCR empty via %s.", sel)
                return solved or ""
            except Exception:
                continue
        logger.info("IGR captcha image selector not found.")
        return ""

    async def _refresh_captcha_image(self) -> None:
        """
        Best-effort captcha refresh before retrying.
        """
        assert self.page is not None
        selectors = (IMG_CAPTCHA, "img[id*='captcha' i]", "img[src*='captcha' i]")
        for sel in selectors:
            try:
                img = self.page.locator(sel).first
                if await img.count() == 0:
                    continue
                # Many portals refresh captcha on image click.
                await img.click(timeout=1200)
                await asyncio.sleep(0.8)
                # Force cache-bust as fallback.
                await self.page.evaluate(
                    """(selector) => {
                        const el = document.querySelector(selector);
                        if (!el || !el.src) return false;
                        const base = el.src.split('?')[0];
                        el.src = base + '?_ts=' + Date.now();
                        return true;
                    }""",
                    sel,
                )
                await asyncio.sleep(0.8)
                return
            except Exception:
                continue

    async def _get_captcha_src_fingerprint(self) -> str:
        """
        Read current captcha image src as a simple fingerprint.
        """
        assert self.page is not None
        selectors = (IMG_CAPTCHA, "img[id*='captcha' i]", "img[src*='captcha' i]")
        for sel in selectors:
            try:
                val = await self.page.eval_on_selector(
                    sel,
                    "el => (el && el.getAttribute('src')) ? String(el.getAttribute('src')) : ''",
                )
                if val:
                    return val
            except Exception:
                continue
        return ""

    _PAGE_LOADING_JS = """() => {
        const prm = (window.Sys && window.Sys.WebForms && window.Sys.WebForms.PageRequestManager)
            ? window.Sys.WebForms.PageRequestManager.getInstance()
            : null;
        if (prm && prm.get_isInAsyncPostBack()) return true;
        const progressIds = ['UpdateProgress1', 'UpdateProgress4'];
        return progressIds.some((id) => {
            const el = document.getElementById(id);
            if (!el) return false;
            const cs = window.getComputedStyle(el);
            if (!cs) return false;
            return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
        });
    }"""

    async def _is_igr_page_loading(self) -> bool:
        """True while ASP.NET async postback or UpdateProgress overlay is active."""
        assert self.page is not None
        try:
            return bool(await self.page.evaluate(self._PAGE_LOADING_JS))
        except Exception:
            return False

    async def _wait_for_postback_settle(self, timeout_s: float = 20.0) -> None:
        """
        Wait for ASP.NET async postback/progress overlays to fully settle.
        """
        assert self.page is not None
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                if not await self._is_igr_page_loading():
                    return
            except Exception:
                pass
            await asyncio.sleep(0.25)

    @staticmethod
    def _captcha_status_indicates_rejection(status_text: str) -> bool:
        status = (status_text or "").strip().lower()
        if not status:
            return False
        reject_markers = (
            "incorrect captcha",
            "invalid captcha",
            "wrong captcha",
            "enter correct captcha",
            "please enter correct captcha",
        )
        if any(marker in status for marker in reject_markers):
            return True
        # Phase-1 acceptance copy — not a rejection.
        if "entered correct captcha" in status:
            return False
        if "captcha" in status and any(word in status for word in ("invalid", "incorrect", "wrong", "mismatch")):
            return True
        return False

    @staticmethod
    def _html_indicates_zero_results(html: str) -> bool:
        if _IGR_ZERO_RESULTS_PHRASE in (html or ""):
            return True
        lower = (html or "").lower()
        return any(marker in lower for marker in _IGR_ZERO_RESULT_EN_MARKERS)

    @staticmethod
    def _classify_igr_search_html(
        html: str,
        *,
        previous_captcha_fp: str = "",
        current_captcha_fp: str = "",
        status_text: str = "",
    ) -> str:
        """
        Classify post-submit page state.

        Returns one of: grid, zero, phase1, wrong_captcha, pending.
        """
        if IGRFreeSearchScraper._html_indicates_zero_results(html):
            return "zero"
        grid_rows = IGRFreeSearchScraper._parse_registration_grid(html)
        _, pager_pages = IGRFreeSearchScraper._registration_grid_pager_pages(html)
        if grid_rows or pager_pages:
            return "grid"
        if IGRFreeSearchScraper._captcha_status_indicates_rejection(status_text):
            return "wrong_captcha"
        if (
            previous_captcha_fp
            and current_captcha_fp
            and previous_captcha_fp != current_captcha_fp
        ):
            return "phase1"
        return "pending"

    async def _read_captcha_status_text(self) -> str:
        assert self.page is not None
        try:
            return (
                await self.page.locator("#lblimg_new").first.inner_text(timeout=1000)
            ).strip()
        except Exception:
            return ""

    async def _clear_captcha_field(self) -> None:
        assert self.page is not None
        try:
            await self.page.fill(TXT_CAPTCHA, "")
        except Exception:
            pass

    async def _wait_for_igr_search_outcome(
        self,
        captcha_fp_before: str,
        *,
        timeout_s: float | None = None,
    ) -> tuple[str, str]:
        """
        Poll until the portal shows results, zero-results text, captcha rotation, or timeout.

        Unlike spinner-only waits, this does not treat a missing UpdateProgress1 element
        as “search complete”.
        """
        assert self.page is not None
        wait_s = timeout_s if timeout_s is not None else IGR_RESULTS_WAIT_SECONDS
        deadline = asyncio.get_event_loop().time() + wait_s
        elapsed_s = 0
        last_html = ""
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1.0)
            elapsed_s += 1
            await self._wait_for_postback_settle(timeout_s=3.0)
            cur_fp = await self._get_captcha_src_fingerprint()
            status_text = await self._read_captcha_status_text()
            last_html = await self.page.content()
            outcome = self._classify_igr_search_html(
                last_html,
                previous_captcha_fp=captcha_fp_before,
                current_captcha_fp=cur_fp,
                status_text=status_text,
            )
            if outcome != "pending":
                logger.info(
                    "IGR search outcome=%s after %ds (waited up to %.0fs).",
                    outcome,
                    elapsed_s,
                    wait_s,
                )
                return outcome, last_html
            still_loading = await self._is_igr_page_loading()
            if (
                IGR_PENDING_STALL_SECONDS > 0
                and elapsed_s >= IGR_PENDING_STALL_SECONDS
                and not self._page_html_has_registration_grid(last_html)
                and not still_loading
            ):
                logger.info(
                    "IGR no grid/zero/captcha change after %ds (loading=%s captcha_status=%r) — treating year as empty.",
                    elapsed_s,
                    still_loading,
                    status_text[:80] if status_text else "",
                )
                return "pending_stall", last_html
            if (
                IGR_PENDING_STALL_SECONDS > 0
                and elapsed_s >= IGR_PENDING_STALL_SECONDS
                and still_loading
                and elapsed_s in (int(IGR_PENDING_STALL_SECONDS), int(IGR_PENDING_STALL_SECONDS) + 10)
            ):
                logger.info(
                    "IGR still loading after %ds (UpdateProgress/async postback active) — continuing to wait up to %.0fs.",
                    elapsed_s,
                    wait_s,
                )
            if elapsed_s in (10, 20, 30, 40) or (elapsed_s % 15 == 0 and elapsed_s <= int(wait_s)):
                logger.info(
                    "IGR still waiting for RegistrationGrid or zero-results text… %ds elapsed.",
                    elapsed_s,
                )
        return "timeout", last_html

    @staticmethod
    def _page_html_has_registration_grid(html: str) -> bool:
        rows = IGRFreeSearchScraper._parse_registration_grid(html)
        _, pager_pages = IGRFreeSearchScraper._registration_grid_pager_pages(html)
        return bool(rows or pager_pages)

    async def _wait_for_captcha_image_ready(self, timeout_s: float = 15.0) -> bool:
        """Wait until the captcha image is visible with non-trivial dimensions."""
        assert self.page is not None
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            fp = await self._get_captcha_src_fingerprint()
            if not fp:
                await asyncio.sleep(0.35)
                continue
            try:
                img = self.page.locator(IMG_CAPTCHA).first
                if await img.count() == 0:
                    await asyncio.sleep(0.35)
                    continue
                await img.wait_for(state="visible", timeout=1500)
                box = await img.bounding_box()
                if box and box.get("width", 0) > 10 and box.get("height", 0) > 10:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.35)
        return False

    async def _search_form_looks_ready(self) -> bool:
        """Best-effort check that the Rest-of-MH search form is usable."""
        assert self.page is not None
        try:
            year_val = (await self.page.input_value(SEL_YEAR)).strip()
            survey_val = (await self.page.input_value(TXT_SURVEY)).strip()
            if not year_val or not survey_val:
                return False
        except Exception:
            return False
        return await self._wait_for_captcha_image_ready(timeout_s=4.0)

    async def _reload_portal_search_tab(self) -> None:
        """Navigate to IGR home and open the Rest-of-Maharashtra search tab."""
        assert self.page is not None
        await self._navigate_to_portal()
        await asyncio.sleep(0.5)
        await self._close_startup_popup()
        await self._switch_to_rest_of_maharashtra_tab()

    async def _ensure_rest_maharashtra_form_ready(self, timeout_s: float = 20.0) -> None:
        """Wait until Rest-of-MH district dropdown is present."""
        assert self.page is not None
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                if await self.page.locator(SEL_DISTRICT).count() > 0:
                    return
            except Exception:
                pass
            await self._close_startup_popup()
            await self._switch_to_rest_of_maharashtra_tab()
            await asyncio.sleep(0.5)
        raise RuntimeError("IGR Rest-of-Maharashtra form not ready (district dropdown missing).")

    async def list_location_options(self, level: str) -> list[dict[str, str]]:
        """Return district/taluka/village dropdown options from the IGR search form."""
        from api.location_labels import is_placeholder_label

        assert self.page is not None
        await self._ensure_rest_maharashtra_form_ready()
        selector = {
            "district": SEL_DISTRICT,
            "taluka": SEL_TALUKA,
            "village": SEL_VILLAGE,
        }.get(level)
        if not selector:
            raise ValueError(f"Unknown location level: {level!r}")
        options = await self._get_select_options(selector)
        return [o for o in options if not is_placeholder_label(o.get("label", ""))]

    async def _hard_reset_igr_search_form(
        self,
        *,
        district_label: str,
        taluka_label: str,
        village_label: str,
        survey_number: str,
        year: str,
        reason: str,
    ) -> None:
        """Reload the IGR portal and refill the search form (fast recovery path)."""
        assert self.page is not None
        logger.warning("IGR hard reset search form (%s).", reason)
        await self._reload_portal_search_tab()
        await self._fill_search_form(
            district_label=district_label,
            taluka_label=taluka_label,
            village_label=village_label,
            survey_number=survey_number,
            year=year,
        )
        await self._clear_captcha_field()
        await self._wait_for_captcha_image_ready(timeout_s=12.0)
        logger.info(
            "IGR hard reset complete (captcha_ready fp=%r).",
            (await self._get_captcha_src_fingerprint())[:80],
        )

    async def _recover_search_page_after_stall(
        self,
        *,
        district_label: str,
        taluka_label: str,
        village_label: str,
        survey_number: str,
        year: str,
        reason: str,
        previous_captcha_fp: str = "",
    ) -> None:
        """Leave a hung post-search state via portal reload + form refill."""
        _ = previous_captcha_fp
        await self._hard_reset_igr_search_form(
            district_label=district_label,
            taluka_label=taluka_label,
            village_label=village_label,
            survey_number=survey_number,
            year=year,
            reason=reason,
        )

    async def _prepare_captcha_retry(
        self,
        previous_fp: str,
        *,
        district_label: str = "",
        taluka_label: str = "",
        village_label: str = "",
        survey_number: str = "",
        year: str = "",
        recovery_reason: str = "captcha retry",
    ) -> None:
        """Clear captcha input and force a fresh image before the next submit."""
        form_kwargs = {
            "district_label": district_label,
            "taluka_label": taluka_label,
            "village_label": village_label,
            "survey_number": survey_number,
            "year": year,
        }
        has_form = all(form_kwargs.values())

        await self._clear_captcha_field()
        refreshed = await self._refresh_captcha_until_changed(previous_fp or "", timeout_s=10.0)
        if refreshed:
            return

        logger.warning(
            "IGR captcha image did not rotate after refresh (fp=%r).",
            (previous_fp or "")[:80],
        )
        if has_form:
            await self._recover_search_page_after_stall(
                **form_kwargs,
                reason=f"{recovery_reason} (captcha refresh failed)",
                previous_captcha_fp=previous_fp,
            )

    async def _refresh_captcha_until_changed(self, previous_fp: str, timeout_s: float = 8.0) -> bool:
        """
        Force captcha refresh and wait until captcha fingerprint changes.
        """
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout_s:
            await self._refresh_captcha_image()
            cur = await self._get_captcha_src_fingerprint()
            if cur and cur != previous_fp:
                return True
            await asyncio.sleep(0.4)
        return False

    async def _click_cancel_for_next_year(self) -> None:
        """
        Reset form between years by clicking cancel (best effort).
        """
        assert self.page is not None
        for sel in (
            BTN_CANCEL,
            "button:has-text('Cancel')",
            "input[value='Cancel']",
            "text=Cancel",
        ):
            try:
                btn = self.page.locator(sel).first
                if await btn.count() == 0:
                    continue
                await btn.click(timeout=2500)
                await self._wait_for_postback_settle(timeout_s=12.0)
                await asyncio.sleep(0.4)
                logger.info("IGR year reset: clicked cancel via %s.", sel)
                return
            except Exception:
                continue
        logger.info("IGR year reset: cancel button not found; continuing without explicit cancel.")

    async def _skip_year_as_empty(self, year: str, reason: str) -> list[dict]:
        """Skip this registration year and reload the portal for the next year."""
        logger.info("IGR year=%r skipped as empty (%s).", year, reason)
        await self._click_cancel_for_next_year()
        try:
            await self._reload_portal_search_tab()
            logger.info("IGR portal reloaded after skipping year=%r.", year)
        except Exception as exc:
            logger.warning("IGR portal reload after skip failed: %s", exc)
        return []

    async def _fill_captcha_field(self, captcha_text: str) -> bool:
        """
        Fill the exact captcha textbox and verify the value was set.
        """
        assert self.page is not None
        value = self._normalize_captcha_text(captcha_text or "")
        if not value:
            return False
        try:
            captcha_input = self.page.locator(TXT_CAPTCHA).first
            await captcha_input.fill(value, timeout=4000)
            read_back = (await captcha_input.input_value(timeout=2000)).strip()
            if read_back.lower() == value.lower():
                return True
        except Exception:
            pass

        # JS fallback targets the known textbox id first.
        try:
            await self.page.evaluate(
                """(captcha) => {
                    const el = document.querySelector("#txtImg1")
                        || document.querySelector("input[name='txtImg1']")
                        || document.querySelector("input[name*='captcha' i], input[id*='captcha' i]");
                    if (!el) return;
                    el.value = '';
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.value = captcha;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                value,
            )
            read_back = (await self.page.locator(TXT_CAPTCHA).first.input_value(timeout=2000)).strip()
            return read_back.lower() == value.lower()
        except Exception:
            return False

    @staticmethod
    def _normalize_captcha_text(text: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", (text or "")).upper()
        if len(cleaned) > 6:
            cleaned = cleaned[:6]
        return cleaned

    @staticmethod
    def _expand_needles(label: str) -> list[str]:
        from api.location_labels import expand_label_needles

        return expand_label_needles(label)

    @staticmethod
    def _match_option_label(option_label: str, wanted: str) -> bool:
        from api.location_labels import labels_match

        return labels_match(option_label, wanted)

    async def _select_by_label_alias(self, selector: str, desired_label: str) -> bool:
        from api.location_labels import best_option_match, is_placeholder_label, sanitize_label

        assert self.page is not None
        options = await self._get_select_options(selector)
        usable = [
            o
            for o in options
            if sanitize_label(o.get("label", "")) and not is_placeholder_label(o.get("label", ""))
        ]
        match = best_option_match(desired_label, usable)
        if match is None:
            return False
        try:
            if match.value:
                await self.page.select_option(selector, value=match.value)
            else:
                await self.page.select_option(selector, label=match.label)
            await asyncio.sleep(0.25)
            return True
        except Exception:
            return False

    async def _get_select_options(self, selector: str) -> list[dict[str, str]]:
        assert self.page is not None
        return await self.page.eval_on_selector(
            selector,
            """sel => Array.from(sel.options).map(o => ({
                value: (o.value || '').trim(),
                label: (o.textContent || '').trim()
            }))""",
        )

    async def _wait_for_option_growth(self, selector: str, min_count: int = 2, timeout_s: float = 8.0) -> None:
        assert self.page is not None
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                count = await self.page.locator(f"{selector} option").count()
                if count >= min_count:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)

    async def _wait_for_select_populated(self, selector: str, timeout_s: float = 10.0) -> None:
        """
        Wait until a select has at least one meaningful option with a value.
        """
        assert self.page is not None
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                populated = await self.page.eval_on_selector(
                    selector,
                    """(sel) => {
                        const opts = Array.from(sel.options || []);
                        return opts.some((o) => {
                            const v = (o.value || '').trim();
                            const t = (o.textContent || '').trim();
                            if (!v) return false;
                            if (t.startsWith('--')) return false;
                            return true;
                        });
                    }""",
                )
                if populated:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)

    async def _fill_search_form(
        self,
        district_label: str,
        taluka_label: str,
        village_label: str,
        survey_number: str,
        year: str,
    ) -> None:
        """
        Re-fill all search fields for each year/survey attempt.
        """
        assert self.page is not None
        district_ok = taluka_ok = village_ok = False
        year_ok = False
        # Retry once if tab/form drift caused dropdown reset.
        for pass_idx in range(2):
            # Year + district/taluka/village are explicit ASP.NET selects.
            try:
                await self.page.select_option(SEL_YEAR, value=year)
                year_ok = True
            except Exception:
                year_ok = await self._select_by_label_alias(SEL_YEAR, year)
            district_ok = await self._select_by_label_alias(SEL_DISTRICT, district_label)
            await self._wait_for_postback_settle(timeout_s=12.0)
            await self._wait_for_option_growth(SEL_TALUKA, min_count=2)
            await self._wait_for_select_populated(SEL_TALUKA, timeout_s=12.0)
            taluka_ok = await self._select_by_label_alias(SEL_TALUKA, taluka_label)
            await self._wait_for_postback_settle(timeout_s=12.0)
            await self._wait_for_option_growth(SEL_VILLAGE, min_count=2)
            await self._wait_for_select_populated(SEL_VILLAGE, timeout_s=12.0)
            village_ok = await self._select_by_label_alias(SEL_VILLAGE, village_label)
            if district_ok and taluka_ok and village_ok:
                break
            if pass_idx == 0:
                logger.info(
                    "IGR form fill pass1 failed; re-selecting Rest of Maharashtra tab and retrying."
                )
                await self._close_startup_popup()
                await self._switch_to_rest_of_maharashtra_tab()
                await asyncio.sleep(0.4)

        # Property number / survey no.
        try:
            await self.page.fill(TXT_SURVEY, survey_number)
        except Exception:
            await self.page.evaluate(
                """(v) => {
                    const el = document.querySelector("input[name='txtAttributeValue1'], #txtAttributeValue1, input[name*='survey' i], input[id*='survey' i]");
                    if (!el) return;
                    el.value = '';
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.value = v;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                survey_number,
            )

        # Always clear captcha field before solving/refilling.
        try:
            await self.page.fill(TXT_CAPTCHA, "")
        except Exception:
            await self.page.evaluate(
                """() => {
                    const cap = document.querySelector("#txtImg1, input[name='txtImg1'], input[name*='captcha' i], input[id*='captcha' i]");
                    if (!cap) return;
                    cap.value = '';
                    cap.dispatchEvent(new Event('input', {bubbles: true}));
                    cap.dispatchEvent(new Event('change', {bubbles: true}));
                }"""
            )
        # Cascading dropdowns need a brief settle time after change events.
        await asyncio.sleep(0.35)

        snapshot: dict[str, str] = {}
        try:
            snapshot = await self.page.evaluate(
                """() => {
                    const pick = (keys) => {
                        for (const k of keys) {
                            const el = document.querySelector(k);
                            if (!el) continue;
                            if (el.tagName === 'SELECT') {
                                const opt = el.options[el.selectedIndex];
                                return (opt && (opt.textContent || '').trim()) || '';
                            }
                            return (el.value || '').trim();
                        }
                        return '';
                    };
                    return {
                        district: pick(["#ddlDistrict1", "select[name*='district' i]", "select[id*='district' i]"]),
                        taluka: pick(["#ddltahsil", "select[name*='taluka' i]", "select[id*='taluka' i]"]),
                        village: pick(["#ddlvillage", "select[name*='village' i]", "select[id*='village' i]"]),
                        survey: pick(["#txtAttributeValue1", "input[name*='survey' i]", "input[id*='survey' i]"]),
                        year: pick(["#ddlFromYear1", "select[name*='year' i]", "select[id*='year' i]"]),
                    };
                }"""
            )
        except Exception:
            snapshot = {}
        logger.info(
            "IGR form filled snapshot: %s (status: year=%s district=%s taluka=%s village=%s)",
            snapshot,
            year_ok,
            district_ok,
            taluka_ok,
            village_ok,
        )

        if not (district_ok and taluka_ok and village_ok):
            # Form fields are completely blank — the page has likely drifted to a
            # blank / error state (common on the second shared-browser context after
            # a postback failure).  Navigate back to the portal before raising so
            # that the outer _run_with_retries gets a clean page on its next attempt.
            try:
                await self._navigate_to_portal()
                await asyncio.sleep(0.8)
                await self._close_startup_popup()
                await self._switch_to_rest_of_maharashtra_tab()
            except Exception:
                pass
            raise RuntimeError(
                "IGR form fill failed: district/taluka/village not selected "
                f"(district_ok={district_ok}, taluka_ok={taluka_ok}, village_ok={village_ok}, snapshot={snapshot})"
            )

    async def _read_form_snapshot(self) -> dict[str, str]:
        assert self.page is not None
        try:
            return await self.page.evaluate(
                """() => {
                    const pick = (keys) => {
                        for (const k of keys) {
                            const el = document.querySelector(k);
                            if (!el) continue;
                            if (el.tagName === 'SELECT') {
                                const opt = el.options[el.selectedIndex];
                                return (opt && (opt.textContent || '').trim()) || '';
                            }
                            return (el.value || '').trim();
                        }
                        return '';
                    };
                    return {
                        district: pick(["#ddlDistrict1", "select[name*='district' i]", "select[id*='district' i]"]),
                        taluka: pick(["#ddltahsil", "select[name*='taluka' i]", "select[id*='taluka' i]"]),
                        village: pick(["#ddlvillage", "select[name*='village' i]", "select[id*='village' i]"]),
                        survey: pick(["#txtAttributeValue1", "input[name*='survey' i]", "input[id*='survey' i]"]),
                        year: pick(["#ddlFromYear1", "select[name*='year' i]", "select[id*='year' i]"]),
                    };
                }"""
            )
        except Exception:
            return {}

    def _snapshot_matches_expected(
        self,
        snap: dict[str, str],
        district_label: str,
        taluka_label: str,
        village_label: str,
        survey_number: str,
        year: str,
    ) -> bool:
        district_ok = self._match_option_label(snap.get("district", ""), district_label)
        taluka_ok = self._match_option_label(snap.get("taluka", ""), taluka_label)
        village_ok = self._match_option_label(snap.get("village", ""), village_label)
        survey_ok = (snap.get("survey", "").strip() == (survey_number or "").strip())
        year_ok = (snap.get("year", "").strip() == (year or "").strip())
        return district_ok and taluka_ok and village_ok and survey_ok and year_ok

    @staticmethod
    def _extract_survey_refs(text: str) -> list[str]:
        """
        Extract survey-like references from row text.
        Examples: 70/4, 1530/2, 70/7/1, 70/7पै
        """
        if not text:
            return []
        hits = re.findall(r"\b\d+(?:/\d+)*(?:/[^\s,;:()\[\]{}]+)?\b", text)
        out: list[str] = []
        seen: set[str] = set()
        for h in hits:
            if "/" not in h:
                continue
            key = h.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(h.strip())
        return out

    @staticmethod
    def _is_placeholder_result_row(row: dict) -> bool:
        row_text = ((row.get("_row_text") or "") + " " + json.dumps(row, ensure_ascii=False)).lower()
        if not row_text.strip():
            return True
        if "disclaimer" in row_text or "feedback[at]igrmaharashtra" in row_text:
            return True
        if "मिळकत निहाय/property details" in row_text or "दस्त निहाय/document number" in row_text:
            return True
        # Menu rows without detail grid columns should be ignored.
        if "property details" in row_text and "docno" not in row_text and "seller name" not in row_text:
            return True
        if "document number" in row_text and "docno" not in row_text and "seller name" not in row_text:
            return True
        return False

    @staticmethod
    def _meaningful_result_rows(rows: list[dict]) -> list[dict]:
        return [r for r in rows if not IGRFreeSearchScraper._is_placeholder_result_row(r)]

    _REGISTRATION_GRID_PAGE_RE = re.compile(r"RegistrationGrid','Page\$(\d+)'")

    @staticmethod
    def _registration_grid_row_key(row: dict) -> str:
        prop = (
            row.get("Property Description")
            or row.get("PropertyDescription")
            or row.get("property description")
            or ""
        )
        return "|".join(
            [
                str(row.get("DocNo") or row.get("Doc No") or "").strip(),
                str(row.get("RDate") or row.get("R Date") or "").strip(),
                prop.strip()[:160],
            ]
        )

    @staticmethod
    def _registration_grid_pager_pages(html: str) -> tuple[int | None, list[int]]:
        """Return current page number and all page numbers linked in the RegistrationGrid pager."""
        soup = BeautifulSoup(html or "", "html.parser")
        grid = soup.find("table", id="RegistrationGrid")
        if not grid:
            return None, []

        current: int | None = None
        linked: set[int] = set()
        for pager_table in grid.find_all("table"):
            for cell in pager_table.find_all(["span", "a"]):
                text = cell.get_text(strip=True)
                if text.isdigit():
                    if cell.name == "span":
                        current = int(text)
                    elif cell.name == "a":
                        linked.add(int(text))
                href = cell.get("href") if cell.name == "a" else None
                if href:
                    for m in IGRFreeSearchScraper._REGISTRATION_GRID_PAGE_RE.finditer(href):
                        linked.add(int(m.group(1)))

        if current is not None:
            linked.add(current)
        return current, sorted(linked)

    @staticmethod
    def _parse_registration_grid(html: str) -> list[dict]:
        """Parse document rows from the IGR RegistrationGrid table (excludes pager/footer row)."""
        soup = BeautifulSoup(html or "", "html.parser")
        grid = soup.find("table", id="RegistrationGrid")
        if not grid:
            return []

        rows = grid.find_all("tr")
        if len(rows) < 2:
            return []

        headers = [h.get_text(" ", strip=True) for h in rows[0].find_all(["th", "td"])]
        out: list[dict] = []
        for r_index, tr in enumerate(rows[1:], start=1):
            if tr.find("table"):  # pager row embeds inner table
                continue
            tds = tr.find_all("td")
            if not tds:
                continue
            rec: dict[str, str] = {}
            for i, td in enumerate(tds):
                key = headers[i] if i < len(headers) and headers[i] else f"col_{i}"
                rec[key] = td.get_text(" ", strip=True)
            if not any(v for v in rec.values()):
                continue
            row_text = " | ".join(v for v in rec.values() if v).strip()
            if not row_text or "docno" in row_text.lower() and "dname" in row_text.lower():
                continue
            rec["_row_text"] = row_text
            rec["_survey_refs"] = ",".join(IGRFreeSearchScraper._extract_survey_refs(row_text))
            rec["_table_index"] = "RegistrationGrid"
            rec["_row_index"] = str(r_index)
            out.append(rec)
        return out

    async def _go_to_registration_grid_page(self, page_num: int) -> None:
        assert self.page is not None
        target = str(page_num)
        clicked = False
        try:
            loc = self.page.locator(f'a[href*="Page${target}"]').first
            if await loc.count() > 0:
                await loc.click(timeout=5000)
                clicked = True
        except Exception:
            clicked = False
        if not clicked:
            await self.page.evaluate(
                """(n) => {
                    if (typeof __doPostBack === 'function') {
                        __doPostBack('RegistrationGrid', 'Page$' + n);
                    }
                }""",
                target,
            )
        await self._wait_for_postback_settle(timeout_s=45.0)
        await asyncio.sleep(0.6)

    async def _collect_all_registration_grid_pages(
        self,
        initial_html: str,
        *,
        survey_number: str,
        year: str,
        attempt: int,
    ) -> list[dict]:
        """
        Walk every RegistrationGrid results page via ASP.NET __doPostBack pager links.
        Falls back to a single _parse_result_table pass when RegistrationGrid is absent.
        """
        assert self.page is not None
        max_pages = max(1, int(os.getenv("IGR_MAX_RESULT_PAGES", "100")))
        all_rows: list[dict] = []
        seen_keys: set[str] = set()
        visited_pages: set[int] = set()
        html = initial_html

        while len(visited_pages) < max_pages:
            current, linked = self._registration_grid_pager_pages(html)
            if current is None and not self._parse_registration_grid(html):
                parsed = self._parse_result_table(html)
                logger.info(
                    "IGR pagination: RegistrationGrid not found; single-page parse rows=%s.",
                    len(parsed),
                )
                return parsed

            if current is not None:
                visited_pages.add(current)
            elif linked:
                visited_pages.add(min(linked))

            page_rows = self._parse_registration_grid(html)
            for row in page_rows:
                key = self._registration_grid_row_key(row)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_rows.append(row)

            unvisited = [p for p in linked if p not in visited_pages]
            if not unvisited:
                logger.info(
                    "IGR pagination complete: survey=%r year=%r pages=%s rows=%s.",
                    survey_number,
                    year,
                    len(visited_pages),
                    len(all_rows),
                )
                break

            next_page = min(unvisited)
            visited_pages.add(next_page)
            logger.info(
                "IGR pagination: visiting page %s (linked=%s..%s rows so far=%s).",
                next_page,
                min(linked) if linked else next_page,
                max(linked) if linked else next_page,
                len(all_rows),
            )
            await self._go_to_registration_grid_page(next_page)
            html = await self.page.content()
            flag = (os.getenv("IGR_SAVE_RAW_HTML") or "").strip().lower()
            if flag in ("1", "true", "yes", "on"):
                out_dir = Path(os.getenv("IGR_RAW_HTML_DIR", "artifacts/igr_debug"))
                out_dir.mkdir(parents=True, exist_ok=True)
                safe_survey = re.sub(r"[^\w.-]+", "_", survey_number or "unknown")
                out_path = out_dir / f"igr_{year}_{safe_survey}_attempt{attempt}_page{next_page}.html"
                out_path.write_text(html or "", encoding="utf-8")
                logger.info("IGR raw HTML saved (page %s): %s", next_page, out_path)

        if len(visited_pages) >= max_pages:
            logger.warning(
                "IGR pagination stopped at IGR_MAX_RESULT_PAGES=%s (survey=%r year=%r).",
                max_pages,
                survey_number,
                year,
            )
        return all_rows

    @staticmethod
    def _save_raw_search_html(
        html: str,
        *,
        survey_number: str,
        year: str,
        attempt: int,
    ) -> Path | None:
        """
        When IGR_SAVE_RAW_HTML=1, persist the full page HTML captured immediately
        after Search submit and *before* BeautifulSoup table parsing.
        """
        flag = (os.getenv("IGR_SAVE_RAW_HTML") or "").strip().lower()
        if flag not in ("1", "true", "yes", "on"):
            return None
        out_dir = Path(os.getenv("IGR_RAW_HTML_DIR", "artifacts/igr_debug"))
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_survey = re.sub(r"[^\w.-]+", "_", survey_number or "unknown")
        out_path = out_dir / f"igr_{year}_{safe_survey}_attempt{attempt}.html"
        out_path.write_text(html or "", encoding="utf-8")
        logger.info("IGR raw HTML saved (pre-parse): %s (%s bytes)", out_path, len(html or ""))
        return out_path

    @staticmethod
    def _parse_result_table(html: str) -> list[dict]:
        """
        Parse all visible tables in result area, not just first table.
        Includes raw row text + detected survey refs for sibling filtering.
        """
        soup = BeautifulSoup(html or "", "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return []

        out: list[dict] = []
        for t_index, table in enumerate(tables):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [h.get_text(" ", strip=True) for h in rows[0].find_all(["th", "td"])]
            for r_index, tr in enumerate(rows[1:], start=1):
                tds = tr.find_all("td")
                if not tds:
                    continue
                rec: dict[str, str] = {}
                for i, td in enumerate(tds):
                    key = headers[i] if i < len(headers) and headers[i] else f"col_{i}"
                    rec[key] = td.get_text(" ", strip=True)
                if not any(v for v in rec.values()):
                    continue
                row_text = " | ".join(v for v in rec.values() if v).strip()
                rec["_row_text"] = row_text
                rec["_survey_refs"] = ",".join(IGRFreeSearchScraper._extract_survey_refs(row_text))
                rec["_table_index"] = str(t_index)
                rec["_row_index"] = str(r_index)
                out.append(rec)
        return out

    async def search_rest_maharashtra(
        self,
        district_label: str,
        taluka_label: str,
        village_label: str,
        survey_number: str,
        year: str,
    ) -> list[dict]:
        """
        Execute one search attempt and parse visible result table.

        NOTE: selectors on IGR portal are dynamic and may drift; this function
        uses broad selectors and is intentionally defensive.
        """
        assert self.page is not None
        logger.info(
            "IGR search start: district=%r taluka=%r village=%r survey=%r year=%r "
            "(results_wait=%.0fs pending_stall=%.0fs)",
            district_label,
            taluka_label,
            village_label,
            survey_number,
            year,
            IGR_RESULTS_WAIT_SECONDS,
            IGR_PENDING_STALL_SECONDS,
        )
        from api.location_labels import resolve_igr_labels

        mapped_d, mapped_t, mapped_v, map_method = resolve_igr_labels(
            district_label, taluka_label, village_label
        )
        if map_method:
            logger.info(
                "IGR location map applied (%s): bhulekh=%r/%r/%r -> igr=%r/%r/%r",
                map_method,
                district_label,
                taluka_label,
                village_label,
                mapped_d,
                mapped_t,
                mapped_v,
            )
            district_label, taluka_label, village_label = mapped_d, mapped_t, mapped_v
        # If the page has drifted off the portal (blank page, error page, or
        # a previous navigation left it somewhere unexpected), reload it now so
        # _fill_search_form starts from a known-good state.
        try:
            current_url = self.page.url or ""
            page_html = await self.page.content()
            stale_results = self._page_html_has_registration_grid(page_html)
            if stale_results or "freesearchigrservice" not in current_url:
                logger.info(
                    "IGR year=%r: stale page detected (grid=%s url=%r) — reloading portal.",
                    year,
                    stale_results,
                    current_url[:80],
                )
                await self._reload_portal_search_tab()
        except Exception:
            pass
        await self._close_startup_popup()
        await self._fill_search_form(
            district_label=district_label,
            taluka_label=taluka_label,
            village_label=village_label,
            survey_number=survey_number,
            year=year,
        )
        logger.info(
            "IGR sequential fill complete: year -> district -> taluka -> village -> survey (single pass)."
        )

        phase2_attempt = 0
        total_submit = 0
        consecutive_empty_ocr = 0
        max_submits = max(MAX_CAPTCHA_RETRIES * 3, 10)
        form_retry_kwargs = {
            "district_label": district_label,
            "taluka_label": taluka_label,
            "village_label": village_label,
            "survey_number": survey_number,
            "year": year,
        }

        while phase2_attempt < MAX_CAPTCHA_RETRIES and total_submit < max_submits:
            total_submit += 1
            # Ensure non-captcha fields are correctly set before every search attempt.
            current = await self._read_form_snapshot()
            if not self._snapshot_matches_expected(
                current,
                district_label=district_label,
                taluka_label=taluka_label,
                village_label=village_label,
                survey_number=survey_number,
                year=year,
            ):
                logger.info(
                    "IGR submit %s: form drift detected; refilling sequential fields. snapshot=%s",
                    total_submit,
                    current,
                )
                await self._fill_search_form(
                    district_label=district_label,
                    taluka_label=taluka_label,
                    village_label=village_label,
                    survey_number=survey_number,
                    year=year,
                )
            fp_before = await self._get_captcha_src_fingerprint()
            solved = await self._solve_captcha()
            if not solved:
                consecutive_empty_ocr += 1
                logger.info(
                    "IGR submit %s: empty OCR text (consecutive=%s).",
                    total_submit,
                    consecutive_empty_ocr,
                )
                if consecutive_empty_ocr >= IGR_EMPTY_OCR_RECOVERY_THRESHOLD:
                    consecutive_empty_ocr = 0
                    await self._recover_search_page_after_stall(
                        **form_retry_kwargs,
                        reason=f"{IGR_EMPTY_OCR_RECOVERY_THRESHOLD} consecutive empty OCR reads",
                        previous_captcha_fp=fp_before,
                    )
                continue
            consecutive_empty_ocr = 0
            filled_ok = await self._fill_captcha_field(solved)
            if not filled_ok:
                logger.info(
                    "IGR submit %s: failed to fill %s with OCR text; retrying.",
                    total_submit,
                    TXT_CAPTCHA,
                )
                continue
            logger.info(
                "IGR submit %s: filled captcha (len=%s).",
                total_submit,
                len(solved),
            )
            clicked = False
            for sel in (BTN_SEARCH, "button:has-text('Search')", "input[type='submit']", "button[type='submit']", "text=Search"):
                try:
                    btn = self.page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click(timeout=2000)
                        logger.info("IGR submit %s: clicked search via %s.", total_submit, sel)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                await self.page.evaluate("document.querySelector('form')?.submit()")
                logger.info("IGR submit %s: fallback form submit().", total_submit)

            await self._wait_for_postback_settle(timeout_s=12.0)

            outcome, html = await self._wait_for_igr_search_outcome(fp_before)

            if outcome == "phase1":
                logger.info(
                    "IGR submit %s: captcha rotated after submit (Phase-1) — solving new captcha.",
                    total_submit,
                )
                await self._wait_for_captcha_image_ready(timeout_s=8.0)
                continue

            if outcome in ("timeout", "pending_stall", "captcha_stall"):
                phase2_attempt += 1
                consecutive_empty_ocr = 0
                self._save_raw_search_html(
                    html,
                    survey_number=survey_number,
                    year=year,
                    attempt=phase2_attempt,
                )
                stall_reason = {
                    "pending_stall": f"no grid after {IGR_PENDING_STALL_SECONDS:.0f}s pending",
                    "captcha_stall": "captcha accepted but no grid",
                }.get(outcome, f"RegistrationGrid not ready within {IGR_RESULTS_WAIT_SECONDS:.0f}s")
                return await self._skip_year_as_empty(year, reason=stall_reason)

            if outcome == "wrong_captcha":
                logger.info(
                    "IGR submit %s: portal rejected captcha — refreshing image.",
                    total_submit,
                )
                await self._prepare_captcha_retry(
                    fp_before,
                    **form_retry_kwargs,
                    recovery_reason="wrong captcha",
                )
                continue

            if outcome == "zero":
                phase2_attempt += 1
                self._save_raw_search_html(
                    html,
                    survey_number=survey_number,
                    year=year,
                    attempt=phase2_attempt,
                )
                return await self._skip_year_as_empty(
                    year,
                    reason="portal confirmed zero results",
                )

            if outcome != "grid":
                phase2_attempt += 1
                logger.warning(
                    "IGR phase-2 attempt %s/%s: unexpected outcome=%r; retrying.",
                    phase2_attempt,
                    MAX_CAPTCHA_RETRIES,
                    outcome,
                )
                await self._prepare_captcha_retry(
                    fp_before,
                    **form_retry_kwargs,
                    recovery_reason=f"unexpected outcome {outcome!r}",
                )
                continue

            phase2_attempt += 1
            self._save_raw_search_html(
                html,
                survey_number=survey_number,
                year=year,
                attempt=phase2_attempt,
            )
            # Portal shows this Marathi phrase when a year genuinely has no records.
            if self._html_indicates_zero_results(html):
                return await self._skip_year_as_empty(
                    year,
                    reason="portal confirmed zero results after grid check",
                )

            parsed = await self._collect_all_registration_grid_pages(
                html,
                survey_number=survey_number,
                year=year,
                attempt=phase2_attempt,
            )
            if parsed:
                meaningful = self._meaningful_result_rows(parsed)
                if not meaningful:
                    return await self._skip_year_as_empty(
                        year,
                        reason="only placeholder/menu rows in RegistrationGrid",
                    )
                logger.info(
                    "IGR search success: survey=%r year=%r rows=%s meaningful=%s (phase2_attempt=%s).",
                    survey_number,
                    year,
                    len(parsed),
                    len(meaningful),
                    phase2_attempt,
                )
                for row in meaningful:
                    row["search_year"] = year
                    row["district_label"] = district_label
                    row["taluka_label"] = taluka_label
                    row["village_label"] = village_label
                    row["survey_number"] = survey_number
                await self._click_cancel_for_next_year()
                return meaningful
            logger.info(
                "IGR phase-2 attempt %s/%s: no result rows parsed.",
                phase2_attempt,
                MAX_CAPTCHA_RETRIES,
            )
            return await self._skip_year_as_empty(
                year,
                reason="RegistrationGrid present but no parseable rows",
            )
        logger.warning(
            "IGR search exhausted attempts: survey=%r year=%r — skipping year.",
            survey_number,
            year,
        )
        return await self._skip_year_as_empty(
            year,
            reason=f"exhausted {MAX_CAPTCHA_RETRIES} phase-2 attempts without results",
        )
