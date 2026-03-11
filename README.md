# llm-provider-update-feed

A self-hosted feed service that tracks **model lifecycle events** (new models,
deprecations, retirements, capability changes) across major LLM providers:
**Google Gemini**, **OpenAI**, **Anthropic**, **Azure OpenAI**, and **AWS Bedrock**.

Built with FastAPI + SQLite. Runs on `localhost` with one command.

---

## Features

- **REST API** — list, create, and collect feed items
- **Web UI** — server-rendered timeline with severity badges and filters
- **Collector framework** — pluggable per-provider scrapers with deduplication
- **OpenAPI docs** — auto-generated at `/docs` and `/redoc`
- **Structured logging** — via `structlog`
- **Docker support** — `Dockerfile` + `docker-compose.yml` included

---

## Project structure

```
llm-provider-update-feed/
├── app/                       # Main application package  ← app/README.md
│   ├── main.py                # FastAPI app + all routes
│   ├── config.py              # Pydantic-settings (env / .env)
│   ├── db.py                  # SQLAlchemy engine & session
│   ├── models.py              # ORM model: model_updates table
│   ├── schemas.py             # Pydantic schemas + fingerprint logic
│   ├── crud.py                # DB read/write operations
│   ├── services/              # Orchestration layer  ← app/services/README.md
│   │   └── collector_service.py   # Runs all collectors, persists results
│   ├── collectors/            # Per-provider scrapers  ← app/collectors/README.md
│   │   ├── base.py            # Abstract BaseCollector + shared RSS helper
│   │   ├── gemini.py          # Google Gemini (RSS + HTML scraping)
│   │   ├── openai.py          # OpenAI (RSS + HTML scraping)
│   │   ├── anthropic.py       # Anthropic Claude (HTML scraping)
│   │   ├── azure.py           # Azure OpenAI (HTML scraping + seed fallback)
│   │   └── aws.py             # AWS Bedrock (RSS + HTML scraping + seed fallback)
│   ├── templates/             # Jinja2 templates  ← app/templates/README.md
│   │   └── index.html         # Server-rendered feed page
│   └── static/                # CSS + JS assets  ← app/static/README.md
│       ├── styles.css
│       └── app.js
├── tests/                     # pytest suite  ← tests/README.md
│   └── test_dedupe.py         # Dedupe + collector parsing tests
├── data/                      # SQLite DB written here at runtime
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── requirements.txt
└── README.md
```

Each subdirectory has its own `README.md` with a detailed file map, component descriptions, data-flow diagrams, and extension guides.

---

## Quick start (local)

### 1. Clone & install

```bash
git clone https://github.com/your-org/llm-provider-update-feed
cd llm-provider-update-feed

uv sync          # creates .venv and installs all deps from uv.lock
```

> **No uv?** Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh`
> or fall back to the classic approach: `python -m venv .venv && pip install -r requirements.txt`

### 2. Configure (optional)

```bash
cp .env.example .env
# Works out of the box – SQLite, no API keys required.
```

### 3. Run

```bash
uv run uvicorn app.main:app --reload
# or: make dev
```

The server starts at **http://127.0.0.1:8000**.

---

## Usage

### Web UI

Open **http://127.0.0.1:8000** in your browser.

- Use the dropdowns to filter by provider, severity, or change type.
- Click **⚡ Major only** to show only new-model releases, retirements, and deprecation announcements (hides general capability updates).
- Click **Run collectors now** to fetch live data from provider docs.
- Items are colour-coded: red = CRITICAL, amber = WARN, blue = INFO.

### OpenAPI docs

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc

---

## API reference

### `GET /health`
```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### `GET /api/updates` — list feed items

```bash
# All items (most recent first)
curl http://localhost:8000/api/updates | python -m json.tool

# Filter by provider + severity
curl "http://localhost:8000/api/updates?provider=openai&severity=CRITICAL"

# Filter by change type and date
curl "http://localhost:8000/api/updates?change_type=RETIREMENT&since=2024-01-01T00:00:00Z"

# Paginate
curl "http://localhost:8000/api/updates?limit=10"
# Use next_cursor from response for next page:
curl "http://localhost:8000/api/updates?limit=10&cursor=2024-06-01T12:00:00Z"
```

**Query parameters**

| Parameter     | Type     | Description                                       |
|---------------|----------|---------------------------------------------------|
| `provider`    | string   | `google` \| `openai` \| `anthropic` \| `azure` \| `aws` |
| `severity`    | string   | `INFO` \| `WARN` \| `CRITICAL`                   |
| `change_type` | string   | `NEW_MODEL` \| `DEPRECATION_ANNOUNCED` \| `RETIREMENT` \| `SHUTDOWN_DATE_CHANGED` \| `CAPABILITY_CHANGED` |
| `since`       | datetime | ISO 8601 – only return items created after this  |
| `major_only`  | bool     | `true` – show only `NEW_MODEL`, `RETIREMENT`, `DEPRECATION_ANNOUNCED` |
| `limit`       | int      | 1–200, default 50                                 |
| `cursor`      | string   | Opaque pagination cursor from previous response   |

### `POST /api/updates` — create an item manually

```bash
curl -X POST http://localhost:8000/api/updates \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "anthropic",
    "product": "anthropic_api",
    "model": "claude-2",
    "change_type": "DEPRECATION_ANNOUNCED",
    "severity": "WARN",
    "title": "Claude 2 deprecated",
    "summary": "Claude 2 is deprecated in favour of Claude 3 Haiku.",
    "source_url": "https://docs.anthropic.com/en/docs/about-claude/models",
    "effective_at": "2025-06-01T00:00:00Z"
  }'
```

Returns **201 Created** with the stored item, or **409 Conflict** if a
duplicate fingerprint is detected.

### `POST /api/collect` — run all collectors

```bash
curl -X POST http://localhost:8000/api/collect | python -m json.tool
# {"added": 7, "skipped": 0, "errors": []}
```

This is safe to call repeatedly – duplicates are silently skipped.

---

## Running tests

```bash
uv run pytest tests/ -v
# or: make test
```

---

## Docker

### Build & run with Docker Compose

```bash
docker compose up --build
```

The service will be available at **http://localhost:8000**.

SQLite data persists in `./data/` on your host.

```bash
# Trigger collection inside the running container
curl -X POST http://localhost:8000/api/collect

# Stop
docker compose down
```

### Individual Docker commands

```bash
docker build -t llm-update-feed .
docker run -p 8000:8000 -v "$(pwd)/data:/app/data" llm-update-feed
```

---

## Collector framework

Each collector lives in `app/collectors/` and extends `BaseCollector`:

```python
class MyProviderCollector(BaseCollector):
    provider_name = "myprovider"

    def collect(self) -> list[ModelUpdateCreate]:
        html = self._fetch("https://provider.com/docs/changelog")
        # parse and return list[ModelUpdateCreate]
        ...
```

Register the new collector in `app/services/collector_service.py`:

```python
_ALL_COLLECTORS: list[type[BaseCollector]] = [
    GeminiCollector,
    OpenAICollector,
    MyProviderCollector,   # add here
    ...
]
```

**Deduplication** is automatic: a SHA-256 fingerprint is computed from
`provider + change_type + model + effective_at + source_url + title`.
Duplicate inserts are silently ignored at the DB level (UNIQUE constraint).

### Collector status

| Provider  | Status      | Source                                                |
|-----------|-------------|-------------------------------------------------------|
| Google    | ✅ Live + seed | `ai.google.dev/gemini-api/docs/deprecations`        |
| OpenAI    | ✅ Live + seed | `platform.openai.com/docs/deprecations`             |
| Anthropic | 🚧 Stub (TODO) | `docs.anthropic.com/en/docs/about-claude/models`   |
| Azure     | 🚧 Stub (TODO) | `learn.microsoft.com/azure/ai-services/openai/…`   |
| AWS       | 🚧 Stub (TODO) | `docs.aws.amazon.com/bedrock/latest/userguide/…`   |

> **Note:** The Gemini and OpenAI pages are partially JS-rendered.
> If live parsing yields no results, the collector automatically falls back to
> a curated set of known seed entries so the feed always has representative data.

---

## Configuration reference

All settings can be overridden via environment variables or `.env`:

| Variable                   | Default                       | Description                  |
|----------------------------|-------------------------------|------------------------------|
| `DATABASE_URL`             | `sqlite:///./data/updates.db` | SQLAlchemy database URL      |
| `HOST`                     | `127.0.0.1`                   | Bind host                    |
| `PORT`                     | `8000`                        | Bind port                    |
| `RELOAD`                   | `true`                        | Uvicorn auto-reload          |
| `LOG_LEVEL`                | `info`                        | Logging level                |
| `COLLECTOR_TIMEOUT_SECONDS`| `30`                          | HTTP timeout per collector   |
| `COLLECTOR_MAX_RETRIES`    | `2`                           | Retry attempts per URL       |
| `DEFAULT_PAGE_LIMIT`       | `50`                          | Default items per page       |
| `MAX_PAGE_LIMIT`           | `200`                         | Maximum items per page       |

---

## Make targets

```
make sync          Install / update deps (uv sync)
make dev           Run dev server (auto-reload)
make run           Run production-like server
make test          Run test suite
make collect       Trigger collector via curl
make lint          Run ruff linter
make docker-build  Build Docker image
make docker-up     Start via Docker Compose
make docker-down   Stop containers
make clean         Remove __pycache__ etc.
```

---

## Further reading

Each directory contains a `README.md` with deeper documentation:

| Path | Contents |
|---|---|
| [`app/README.md`](app/README.md) | Full module map, request lifecycle diagrams, data model, module dependency graph |
| [`app/collectors/README.md`](app/collectors/README.md) | Collector framework, parsing strategies, how to implement a new provider |
| [`app/services/README.md`](app/services/README.md) | Orchestration service, error isolation, how to add new services |
| [`app/static/README.md`](app/static/README.md) | CSS design tokens, component class reference, JS button handler |
| [`app/templates/README.md`](app/templates/README.md) | Jinja2 template structure, context variables, filter behaviour |
| [`tests/README.md`](tests/README.md) | Test architecture, fixture/mock strategy, coverage gaps |

### Check :)
