"""Shared Chromium CLI flags for Playwright launches.

Tuned for scraping workloads: lower idle footprint and bounded caches.

Optional: set ``PLAYWRIGHT_CHROMIUM_EXTRA_ARGS`` to a shell-style string of extra
flags (quoted tokens allowed), e.g. ``--disable-features=Foo``.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path


def project_playwright_browsers_path() -> Path:
    return (Path(__file__).resolve().parent / ".playwright-browsers").resolve()


def ensure_playwright_browsers_path() -> str:
    """Point Playwright at the repo-local browser cache (writable, arch-correct)."""
    project_path = str(project_playwright_browsers_path())
    browsers_path_env = (os.getenv("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if not browsers_path_env or "cursor-sandbox-cache" in browsers_path_env:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = project_path
    return os.environ["PLAYWRIGHT_BROWSERS_PATH"]


def resolve_chromium_executable(*, playwright_executable_path: str = "") -> str | None:
    """Return a Chromium binary path, falling back across CPU arch folders on macOS."""
    explicit = (os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or "").strip()
    if explicit and Path(explicit).exists():
        return explicit

    default = (playwright_executable_path or "").strip()
    if default and Path(default).exists():
        return default

    browsers_root = Path(ensure_playwright_browsers_path())
    if not browsers_root.is_dir():
        return None

    # Playwright sometimes resolves mac-x64 on Apple Silicon; scan installed builds.
    candidates: list[Path] = []
    for pattern in ("**/chrome-headless-shell", "**/chrome"):
        candidates.extend(browsers_root.glob(pattern))
    candidates.sort(key=lambda p: ("arm64" in str(p), p.name == "chrome-headless-shell"), reverse=True)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def resolve_system_chromium_executable() -> str | None:
    candidates = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    )
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def chromium_launch_args() -> list[str]:
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        # Fewer GPU / raster helper paths (especially headless).
        "--disable-gpu",
        "--disable-software-rasterizer",
        # Scrapers do not need extensions, sync, or background network stacks.
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--mute-audio",
        "--no-first-run",
        # Limit media/disk cache growth during long workers (bytes).
        "--disk-cache-size=1048576",
        "--media-cache-size=1048576",
    ]
    extra = (os.getenv("PLAYWRIGHT_CHROMIUM_EXTRA_ARGS") or "").strip()
    if extra:
        args.extend(shlex.split(extra))
    return args
