# ── eCourts Scraper — GCP/Cloud Run friendly Docker image ─────────────────
# Uses python:3.11-slim and installs Playwright Chromium with required libs.

FROM python:3.11-slim

# Install system dependencies required by Playwright's Chromium build
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    libgdk-pixbuf2.0-0 \
    libxshmfence1 \
    fonts-liberation \
    fonts-noto-core \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY . .

# Install uv and project dependencies
# Force opencv-python-headless over full opencv to avoid display/GPU crashes
# in headless Docker (full opencv tries to init display libs and can segfault)
RUN pip install uv --no-cache-dir && \
    uv sync --no-dev --frozen && \
    uv run pip install --force-reinstall opencv-python-headless

# Install Playwright's Chromium browser
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers
RUN uv run playwright install chromium

# Pre-download RapidOCR ONNX models so they're cached in the image.
# Without this, first captcha solve downloads ~15MB at runtime, causing
# a ~60s delay that can trigger Render's health check timeout.
RUN uv run python -c "from rapidocr_onnxruntime import RapidOCR; RapidOCR()"

# Run as non-root in deployed containers.
RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV DEV=0
# ddddocr produces single-character junk on Linux x86_64 Docker for Bhulekh
# captchas; RapidOCR ONNX is pre-baked and platform-stable.
ENV CAPTCHA_SOLVER_MODE=rapidocr_only
ENV ECOURTS_API_SEARCH_PAGE_SIZE=20
ENV ECOURTS_API_CASE_STATUSES=PENDING
ENV ECOURTS_API_JUDICIAL_SECTIONS=CIV
ENV ECOURTS_API_CASE_TYPES=

CMD ["uv", "run", "python", "server.py"]
