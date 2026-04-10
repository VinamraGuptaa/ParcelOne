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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY . .

# Install uv and project dependencies
RUN pip install uv --no-cache-dir && \
    uv sync --no-dev --frozen

# Install Playwright's Chromium browser
RUN uv run playwright install chromium

EXPOSE 8000

CMD ["uv", "run", "python", "server.py"]
