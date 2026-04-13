# ── eCourts Scraper — Render (Docker) deployment ──────────────────────────
# Uses python:3.11-slim and installs Playwright Chromium with all system deps.

FROM python:3.11-slim

# Install system dependencies required by Playwright's Chromium build
RUN apt-get update && apt-get install -y --no-install-recommends \
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
    fonts-liberation \
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
RUN uv run playwright install chromium

# Pre-download RapidOCR ONNX models so they're cached in the image.
# Without this, first captcha solve downloads ~15MB at runtime, causing
# a ~60s delay that can trigger Render's health check timeout.
RUN uv run python -c "from rapidocr_onnxruntime import RapidOCR; RapidOCR()"

EXPOSE 8000

CMD ["uv", "run", "python", "server.py"]
