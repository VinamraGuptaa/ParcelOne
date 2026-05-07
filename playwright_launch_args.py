"""Shared Chromium CLI flags for Playwright launches.

Tuned for scraping workloads: lower idle footprint and bounded caches.

Optional: set ``PLAYWRIGHT_CHROMIUM_EXTRA_ARGS`` to a shell-style string of extra
flags (quoted tokens allowed), e.g. ``--disable-features=Foo``.
"""

from __future__ import annotations

import os
import shlex


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
