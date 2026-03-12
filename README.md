# Foundation Model Update Feed

A FastAPI service that scrapes and aggregates **model lifecycle events** — new models, deprecations, retirements, and capability changes — from five major AI providers. Events are stored in SQLite with SHA-256 deduplication and exposed via a REST API and a server-rendered web UI.

**Providers covered:** Google Gemini · OpenAI · Anthropic · Azure OpenAI · AWS Bedrock

---

## Features

- Collects events from provider documentation pages and RSS feeds
- Deduplicates via SHA-256 fingerprint (provider + change type + model + date + URL + title)
- Cursor-based paginated JSON API with provider/severity/change-type filters
- Server-rendered feed UI with filter dropdowns and one-click collector trigger
- Docker-ready with a non-root multi-stage image and persistent SQLite volume

---

## Project Structure

```
.
├── app/
│   ├── main.py                 # FastAPI app, routes, lifespan
│   ├── config.py               # Settings via pydantic-settings
│   ├── models.py               # SQLAlchemy ORM (model_updates table)
│   ├── schemas.py              # Pydantic schemas, enums, fingerprint logic
│   ├── crud.py                 # DB access layer
│   ├── db.py                   # Engine, session factory, init_db
│   ├── collectors/             # Per-provider scrapers
│   │   ├── base.py             # BaseCollector (HTTP fetch, RSS parser)
│   │   ├── anthropic.py        # Anthropic Claude docs
│   │   ├── aws.py              # AWS Bedrock docs + What's New RSS
│   │   ├── azure.py            # Azure OpenAI docs
│   │   ├── gemini.py           # Google Gemini + Vertex AI docs + RSS
│   │   └── openai.py           # OpenAI docs + blog RSS
│   ├── services/
│   │   └── collector_service.py  # Orchestrates all collectors
│   ├── templates/
│   │   └── index.html          # Jinja2 web UI
│   └── static/
│       ├── app.js              # Collector trigger button logic
│       └── styles.css          # Component styles (CSS variables)
├── tests/
│   └── test_dedupe.py          # Deduplication unit tests
├── data/                       # SQLite DB (gitignored, persisted in Docker)
├── Dockerfile                  # Multi-stage build, non-root user
├── docker-compose.yml          # Single-service compose with volume + healthcheck
├── Makefile                    # Dev, test, lint, Docker convenience targets
├── pyproject.toml              # Project metadata and tool config
└── requirements.txt            # Pinned runtime dependencies
```

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web feed UI with filters |
| `GET` | `/health` | Health check — `{"status": "ok"}` |
| `GET` | `/api/updates` | Paginated JSON feed |
| `POST` | `/api/updates` | Manually create a feed item (409 on duplicate) |
| `POST` | `/api/collect` | Trigger all collectors; returns `{added, skipped, errors}` |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/redoc` | ReDoc UI |

### `GET /api/updates` query parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `provider` | string | Filter by provider (`google`, `openai`, `anthropic`, `azure`, `aws`) |
| `severity` | string | `INFO`, `WARN`, or `CRITICAL` |
| `change_type` | string | `NEW_MODEL`, `DEPRECATION_ANNOUNCED`, `RETIREMENT`, `SHUTDOWN_DATE_CHANGED`, `CAPABILITY_CHANGED` |
| `since` | ISO datetime | Only items announced after this time |
| `cursor` | ISO datetime | Pagination cursor (`next_cursor` from previous response) |
| `limit` | int | Page size (default: 50, max: 200) |

---

## Data Model

All events are stored in the `model_updates` table.

| Column | Description |
|--------|-------------|
| `id` | UUID primary key |
| `provider` | Provider name |
| `product` | Product line (e.g. `gemini_api`, `aws_bedrock`) |
| `model` | Model identifier (nullable) |
| `change_type` | Event category |
| `severity` | `INFO` / `WARN` / `CRITICAL` |
| `title` | Event headline |
| `summary` | Short description |
| `source_url` | Canonical source link |
| `announced_at` / `effective_at` | Event timestamps (nullable) |
| `fingerprint` | SHA-256 dedup key (unique) |
| `raw` | JSON-serialized raw collector output |

---

## Configuration

Settings are loaded from environment variables or a `.env` file.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///./data/updates.db` | SQLite path |
| `HOST` | `127.0.0.1` | Bind host |
| `PORT` | `8000` | Bind port |
| `RELOAD` | `true` | Uvicorn auto-reload |
| `LOG_LEVEL` | `info` | Log verbosity |
| `COLLECTOR_TIMEOUT_SECONDS` | `30` | Per-request HTTP timeout |
| `COLLECTOR_MAX_RETRIES` | `2` | Retry attempts on failure |
| `DEFAULT_PAGE_LIMIT` | `50` | Default page size |
| `MAX_PAGE_LIMIT` | `200` | Max page size |
| `*_SOURCE_URLS` | Per-provider defaults | Override as a JSON array |

---

## app/collectors/

Each collector subclasses `BaseCollector`, which provides:
- `_fetch(url)` — HTTP GET with retry and structured logging
- `_fetch_rss(url)` — RSS XML fetch and parse

Collectors implement `collect() -> list[ModelUpdateCreate]` and never raise — all errors are caught and logged internally.

| Collector | Sources |
|-----------|---------|
| `GeminiCollector` | Deprecation table, changelog, Vertex AI model versions & release notes, Google blog RSS |
| `OpenAICollector` | Deprecations page, changelog, blog RSS |
| `AnthropicCollector` | All-models table, API release notes |
| `AzureCollector` | Retirement/deprecation tables, What's New pages |
| `AWSCollector` | Model lifecycle table, doc history changelog, What's New RSS |

All collectors ship with seed fallback entries. The DB unique constraint on `fingerprint` prevents double-inserts.

### Adding a new provider

1. Create `app/collectors/<provider>.py` subclassing `BaseCollector`.
2. Implement `collect()` returning `list[ModelUpdateCreate]`.
3. Append the class to `_ALL_COLLECTORS` in `app/services/collector_service.py`.

---

## app/services/

`collector_service.py` contains `run_all_collectors(db)`, which:
1. Instantiates all registered collectors
2. Calls `.collect()` on each
3. Persists results via `crud.create_update()`; duplicates (fingerprint collision) increment `skipped`
4. Returns `CollectResult(added, skipped, errors)`

---

## app/templates/ & app/static/

The web UI served at `GET /` renders a server-side Jinja2 template.

- **Filter bar** — provider, severity, change type, and limit dropdowns; auto-submits on change
- **Collector trigger** — "Run collectors now" button calls `POST /api/collect` via `fetch()` and shows added/skipped counts inline
- **Feed cards** — each card shows provider/severity/type badges, model tag, dates, title (linked to source), and summary
- **Styles** — CSS custom properties for severity (`CRITICAL` = red, `WARN` = amber, `INFO` = blue) and per-provider accent colors

---

## Development

**Prerequisites:** Python 3.11+, [`uv`](https://github.com/astral-sh/uv)

```bash
make install        # install dependencies
make dev            # start dev server with auto-reload at http://127.0.0.1:8000
make test           # run pytest
make lint           # ruff check
make collect        # trigger collectors via curl
```

---

## Docker

```bash
make docker-build   # build image
make docker-up      # start container (detached)
make docker-logs    # tail logs
make docker-down    # stop and remove container
```

The compose file binds `./data` to `/app/data` to persist the SQLite database across restarts. The healthcheck polls `GET /health` every 30 seconds.

---

## Tests

`tests/test_dedupe.py` covers the fingerprint deduplication logic — verifying that identical events produce the same fingerprint and that differing fields produce distinct fingerprints.

```bash
make test
# or
uv run pytest tests/ -v
```
