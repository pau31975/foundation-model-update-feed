.PHONY: sync install dev run test lint collect docker-build docker-up docker-down docker-logs clean help

# ── Setup ─────────────────────────────────────────────────────────────────────

sync:              ## Install all dependencies (uv – preferred)
	uv sync

install:           ## Alias for sync
	uv sync

# ── Local development ─────────────────────────────────────────────────────────

dev:               ## Run development server with auto-reload
	uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

run:               ## Run production-like server (no reload)
	uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# ── Testing ───────────────────────────────────────────────────────────────────

test:              ## Run all tests
	uv run pytest tests/ -v

# ── Linting ───────────────────────────────────────────────────────────────────

lint:              ## Run ruff linter (add ruff to dev deps if needed)
	uv run ruff check app/ tests/

# ── Collector ─────────────────────────────────────────────────────────────────

collect:           ## Trigger live data collection via the running server
	curl -s -X POST http://localhost:8000/api/collect | python -m json.tool

# ── Docker ────────────────────────────────────────────────────────────────────

docker-build:      ## Build the Docker image
	docker compose build

docker-up:         ## Start the service via Docker Compose
	docker compose up -d

docker-down:       ## Stop and remove containers
	docker compose down

docker-logs:       ## Follow container logs
	docker compose logs -f

# ── Clean ─────────────────────────────────────────────────────────────────────

clean:             ## Remove caches and generated files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf .pytest_cache htmlcov .coverage

help:              ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
