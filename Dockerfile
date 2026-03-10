# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Non-root user for security
RUN useradd --create-home appuser
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app/ app/

# Writable data directory for SQLite
RUN mkdir -p data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENV DATABASE_URL=sqlite:///./data/updates.db \
    HOST=0.0.0.0 \
    PORT=8000 \
    RELOAD=false \
    LOG_LEVEL=info

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
