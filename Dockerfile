# syntax=docker/dockerfile:1
# Supports linux/amd64 and linux/arm64 (Raspberry Pi 4).
# Build with:
#   docker buildx build --platform linux/amd64,linux/arm64 -t social-scanner:latest .

FROM python:3.12-slim AS base

# System dependencies required by Playwright/Chromium and yt-dlp
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
        libglib2.0-0 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpango-1.0-0 \
        libcairo2 \
        libatspi2.0-0 \
        libx11-6 \
        libxext6 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser + system deps via Playwright
# PLAYWRIGHT_BROWSERS_PATH bakes the browser binary into the image so no
# internet access is needed at container startup.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium

# ─── Final stage ─────────────────────────────────────────────────────────────
FROM base AS final
WORKDIR /app

COPY src/ ./src/
COPY settings.yaml .
# .env is NEVER copied — it is injected at runtime via docker-compose env_file

# Persist DB, sessions, media, profiles across container restarts
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Run as non-root user for security
RUN useradd -m -u 1000 scanner
USER scanner

# Healthcheck: the DB file is created by init_db() on startup — its presence
# confirms the scanner process started and completed initialization.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import sys, pathlib; sys.exit(0 if pathlib.Path('/app/data/social_scanner.db').exists() else 1)"

CMD ["python", "src/main.py"]
