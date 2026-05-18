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
    "karve nagar": ("कर्वेनगर", "म .कर्वेनगर", "karvenagar", "karve nagar"),
    "karvenagar": ("कर्वेनगर", "म .कर्वेनगर", "karve nagar"),
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

    async def _wait_for_postback_settle(self, timeout_s: float = 20.0) -> None:
        """
        Wait for ASP.NET async postback/progress overlays to fully settle.
        """
        assert self.page is not None
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                settled = await self.page.evaluate(
                    """() => {
                        const prm = (window.Sys && window.Sys.WebForms && window.Sys.WebForms.PageRequestManager)
                            ? window.Sys.WebForms.PageRequestManager.getInstance()
                            : null;
                        const inAsync = prm ? prm.get_isInAsyncPostBack() : false;
                        const progressIds = ['UpdateProgress1', 'UpdateProgress4'];
                        const visibleProgress = progressIds.some((id) => {
                            const el = document.getElementById(id);
                            if (!el) return false;
                            const cs = window.getComputedStyle(el);
                            if (!cs) return false;
                            return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
                        });
                        return !inAsync && !visibleProgress;
                    }"""
                )
                if settled:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.25)

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

    async def _fill_captcha_field(self, captcha_text: str) -> bool:
        """
        Fill the exact captcha textbox and verify the value was set.
        """
        assert self.page is not None
        value = self._normalize_captcha_text(captcha_text or "")
        if not value:
            return False
        try:
            await self.page.fill(TXT_CAPTCHA, value)
            read_back = (await self.page.input_value(TXT_CAPTCHA)).strip()
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
            read_back = (await self.page.input_value(TXT_CAPTCHA)).strip()
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
        base = _sanitize_label_input(label).lower()
        if not base:
            return []
        out = [base]
        extra = _LABEL_ALIASES.get(base)
        if extra:
            out.extend(extra)
        return out

    @staticmethod
    def _match_option_label(option_label: str, wanted: str) -> bool:
        o = _sanitize_label_input(option_label).lower()
        if not o:
            return False
        for n in IGRFreeSearchScraper._expand_needles(wanted):
            n = (n or "").strip().lower()
            if not n:
                continue
            if n in o:
                return True
            # Labels may include English in parentheses, e.g. पुणे(Pune)
            for m in re.finditer(r"\(([^)]+)\)", o):
                inner = (m.group(1) or "").strip().lower()
                if not inner:
                    continue
                if n == inner or n in inner:
                    return True
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

    async def _select_by_label_alias(self, selector: str, desired_label: str) -> bool:
        assert self.page is not None
        needles = self._expand_needles(desired_label)
        if not needles:
            return False
        try:
            selected = await self.page.eval_on_selector(
                selector,
                """(sel, needles) => {
                    const norm = (s) => (s || '').toString().trim().toLowerCase();
                    const wants = (needles || []).map(norm).filter(Boolean);
                    let chosen = null;
                    for (const opt of Array.from(sel.options || [])) {
                        const value = (opt.value || '').trim();
                        const txt = norm(opt.textContent || '');
                        if (!value || txt.startsWith('--')) continue;
                        for (const w of wants) {
                            if (txt === w || txt.includes(w) || w.includes(txt)) {
                                chosen = opt;
                                break;
                            }
                        }
                        if (chosen) break;
                    }
                    if (!chosen) return '';
                    sel.value = chosen.value;
                    sel.dispatchEvent(new Event('input', {bubbles: true}));
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    return (chosen.textContent || '').trim();
                }""",
                needles,
            )
            await asyncio.sleep(0.25)
            return bool(selected)
        except Exception:
            return False

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
            "IGR search start: district=%r taluka=%r village=%r survey=%r year=%r",
            district_label,
            taluka_label,
            village_label,
            survey_number,
            year,
        )
        # If the page has drifted off the portal (blank page, error page, or
        # a previous navigation left it somewhere unexpected), reload it now so
        # _fill_search_form starts from a known-good state.
        try:
            current_url = self.page.url or ""
            if "freesearchigrservice" not in current_url:
                logger.info(
                    "IGR page URL is %r (not portal) — reloading portal before form fill.", current_url
                )
                await self._navigate_to_portal()
                await asyncio.sleep(0.8)
                await self._close_startup_popup()
                await self._switch_to_rest_of_maharashtra_tab()
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

        for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
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
                    "IGR attempt %s/%s: form drift detected; refilling sequential fields. snapshot=%s",
                    attempt,
                    MAX_CAPTCHA_RETRIES,
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
                logger.info("IGR captcha attempt %s/%s: empty OCR text.", attempt, MAX_CAPTCHA_RETRIES)
                continue
            filled_ok = await self._fill_captcha_field(solved)
            if not filled_ok:
                logger.info(
                    "IGR captcha attempt %s/%s: failed to fill %s with OCR text; retrying.",
                    attempt,
                    MAX_CAPTCHA_RETRIES,
                    TXT_CAPTCHA,
                )
                continue
            logger.info(
                "IGR captcha attempt %s/%s: filled captcha (len=%s).",
                attempt,
                MAX_CAPTCHA_RETRIES,
                len(solved),
            )
            clicked = False
            for sel in (BTN_SEARCH, "button:has-text('Search')", "input[type='submit']", "button[type='submit']", "text=Search"):
                try:
                    btn = self.page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click(timeout=2000)
                        logger.info("IGR captcha attempt %s/%s: clicked search via %s.", attempt, MAX_CAPTCHA_RETRIES, sel)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                await self.page.evaluate("document.querySelector('form')?.submit()")
                logger.info("IGR captcha attempt %s/%s: fallback form submit().", attempt, MAX_CAPTCHA_RETRIES)
            await self._wait_for_postback_settle(timeout_s=25.0)
            # Small extra buffer after spinner/discrete postback settles.
            await asyncio.sleep(0.8)

            # Portal sometimes returns a red status message "Entered Correct Captcha"
            # while still staying on the same form. Per workflow requirement, retry captcha.
            try:
                status_text = (
                    await self.page.locator("#lblimg_new").first.inner_text(timeout=1000)
                ).strip()
            except Exception:
                status_text = ""
            if status_text and "entered correct captcha" in status_text.lower():
                logger.info(
                    "IGR captcha attempt %s/%s: status=%r -> retrying captcha/search.",
                    attempt,
                    MAX_CAPTCHA_RETRIES,
                    status_text,
                )
                try:
                    # Preferred behavior: site auto-refreshes captcha after this status.
                    # Wait for that new captcha first, then use it on next loop.
                    changed = False
                    for _ in range(10):
                        cur_fp = await self._get_captcha_src_fingerprint()
                        if cur_fp and cur_fp != fp_before:
                            changed = True
                            break
                        await asyncio.sleep(0.4)
                    if changed:
                        logger.info(
                            "IGR captcha attempt %s/%s: captcha changed after status retry=True",
                            attempt,
                            MAX_CAPTCHA_RETRIES,
                        )
                    else:
                        # Per updated flow: if captcha does not change, refresh page and re-run
                        # the same year as a fresh search form cycle.
                        logger.info(
                            "IGR captcha attempt %s/%s: captcha unchanged after status; refreshing page and refilling form.",
                            attempt,
                            MAX_CAPTCHA_RETRIES,
                        )
                        await self.page.reload(wait_until="domcontentloaded", timeout=IGR_GOTO_TIMEOUT_MS)
                        await asyncio.sleep(0.8)
                        await self._close_startup_popup()
                        await self._switch_to_rest_of_maharashtra_tab()
                        await self._fill_search_form(
                            district_label=district_label,
                            taluka_label=taluka_label,
                            village_label=village_label,
                            survey_number=survey_number,
                            year=year,
                        )
                except Exception:
                    logger.info("IGR captcha refresh best-effort failed; continuing retry.")
                continue

            html = await self.page.content()
            self._save_raw_search_html(
                html,
                survey_number=survey_number,
                year=year,
                attempt=attempt,
            )
            parsed = self._parse_result_table(html)
            if parsed:
                meaningful = self._meaningful_result_rows(parsed)
                if not meaningful:
                    logger.info(
                        "IGR captcha attempt %s/%s: only placeholder/menu rows found; treating as zero-result year.",
                        attempt,
                        MAX_CAPTCHA_RETRIES,
                    )
                    await self._click_cancel_for_next_year()
                    return []
                logger.info(
                    "IGR search success: survey=%r year=%r rows=%s meaningful=%s (attempt=%s).",
                    survey_number,
                    year,
                    len(parsed),
                    len(meaningful),
                    attempt,
                )
                for row in meaningful:
                    row["search_year"] = year
                    row["district_label"] = district_label
                    row["taluka_label"] = taluka_label
                    row["village_label"] = village_label
                    row["survey_number"] = survey_number
                await self._click_cancel_for_next_year()
                return meaningful
            logger.info("IGR captcha attempt %s/%s: no result rows parsed.", attempt, MAX_CAPTCHA_RETRIES)
        logger.info("IGR search exhausted captcha attempts: survey=%r year=%r", survey_number, year)
        await self._click_cancel_for_next_year()
        return []
