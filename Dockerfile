# Stage 1: Build frontend static export
FROM node:20-slim AS frontend-build

WORKDIR /build
COPY frontend/ .
RUN npm ci && npm run build

# Stage 2: Production image with backend + static frontend
FROM python:3.12-slim

WORKDIR /app

# Install uv (pinned for reproducible builds)
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/

# Install Python dependencies first so they stay cached across code changes
COPY backend/pyproject.toml backend/uv.lock backend/.python-version backend/
RUN cd backend && uv sync --frozen --no-dev

# Copy backend source
COPY backend/ backend/

# Copy frontend build output as static files
COPY --from=frontend-build /build/out/ static/

# SQLite lives on the /app/db volume (see docker run -v finally-data:/app/db)
ENV DB_PATH=/app/db/finally.db
RUN mkdir -p /app/db

# Run straight from the synced venv; `uv run` would re-sync (dev group) at startup
ENV PATH="/app/backend/.venv/bin:$PATH"

EXPOSE 8000

WORKDIR /app/backend

# python:3.12-slim has no curl, so probe with the stdlib
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
