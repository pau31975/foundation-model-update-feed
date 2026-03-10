# `app/` — Application Package

This is the main Python package for the **LLM Provider Update Feed** service. It contains all server-side logic: the FastAPI application, database layer, Pydantic schemas, CRUD operations, provider collectors, and the Jinja2 web UI.

---

## File map

| File | Role |
|---|---|
| `__init__.py` | Package marker, no logic |
| `config.py` | All configuration via environment variables / `.env` |
| `db.py` | SQLAlchemy engine, session factory, `Base`, table init |
| `models.py` | ORM model — the `model_updates` table |
| `schemas.py` | Pydantic validation, enums, fingerprint computation |
| `crud.py` | Database read/write operations |
| `main.py` | FastAPI app, all routes, lifespan hooks, logging setup |
| `collectors/` | Per-provider HTML scrapers (see [`collectors/README.md`](collectors/README.md)) |
| `services/` | Collector orchestration (see [`services/README.md`](services/README.md)) |
| `static/` | CSS stylesheet and JavaScript (see [`static/README.md`](static/README.md)) |
| `templates/` | Jinja2 HTML template (see [`templates/README.md`](templates/README.md)) |

---

## System flow

### Web UI request (`GET /`)

```
Browser
  └─► GET / ?provider=openai&severity=CRITICAL
        │
        ▼
    main.py — route handler
        │  builds FeedQuery from query-string params
        │
        ▼
    crud.list_updates(db, query)
        │  SELECT … FROM model_updates WHERE … ORDER BY created_at DESC
        │
        ▼
    Jinja2 TemplateResponse("index.html", context)
        │  injects items, total, enum lists, selected filters
        │
        ▼
    Browser renders feed timeline
```

### JSON API request (`GET /api/updates`)

```
Client
  └─► GET /api/updates ?provider=…&cursor=…
        │
        ▼
    main.py — route handler
        │  validates query params → FeedQuery
        │
        ▼
    crud.list_updates(db, query)  →  (rows, total)
        │
        ▼
    ModelUpdateRead.model_validate(row) for each row
        │
        ▼
    FeedPage { items, total, limit, next_cursor } JSON
```

### Manual item creation (`POST /api/updates`)

```
Client
  └─► POST /api/updates  { body: ModelUpdateCreate }
        │
        ▼
    Pydantic validates body
        │  computes fingerprint (SHA-256)
        │
        ▼
    crud.create_update(db, item)
        │  INSERT INTO model_updates … ON CONFLICT (fingerprint) → skipped
        │
        ├─ new row  →  201 ModelUpdateRead
        └─ duplicate →  409 Conflict
```

### Collector trigger (`POST /api/collect`)

```
Client / Browser
  └─► POST /api/collect
        │
        ▼
    main.py → services.collector_service.run_all_collectors(db)
        │
        ├─► GeminiCollector.collect()   → list[ModelUpdateCreate]
        ├─► OpenAICollector.collect()   → list[ModelUpdateCreate]
        ├─► AnthropicCollector.collect() → [] (stub)
        ├─► AzureCollector.collect()    → [] (stub)
        └─► AWSCollector.collect()      → [] (stub)
              │
              ▼  for each item:
        crud.create_update(db, item)   — dedup via fingerprint
              │
              ▼
        CollectResult { added, skipped, errors }
```

---

## Module dependency graph

```
config.py ◄── db.py ──────────► models.py
    ▲              │                  │
    │              ▼                  ▼
    └───── main.py ──► crud.py ──► schemas.py
               │                      ▲
               ▼                      │
    services/collector_service.py     │
               │                      │
               ▼                      │
    collectors/base.py ◄──────────────┘
               ▲
    ┌──────────┴──────────┐
 gemini.py  openai.py  anthropic.py  azure.py  aws.py
```

---

## Data model — `model_updates` table

Defined in `models.py` as the `ModelUpdate` ORM class.

| Column | Type | Notes |
|---|---|---|
| `id` | `VARCHAR(36)` PK | UUID v4, auto-generated |
| `provider` | `VARCHAR(32)` | `google`, `openai`, `anthropic`, `azure`, `aws` |
| `product` | `VARCHAR(64)` | e.g. `openai_api`, `gemini_api` |
| `model` | `VARCHAR(128)` nullable | Specific model ID, e.g. `gpt-4-0314` |
| `change_type` | `VARCHAR(64)` | `NEW_MODEL`, `DEPRECATION_ANNOUNCED`, `RETIREMENT`, etc. |
| `severity` | `VARCHAR(16)` | `INFO`, `WARN`, `CRITICAL` |
| `title` | `VARCHAR(256)` | Human-readable event headline |
| `summary` | `TEXT` | Description of the change |
| `source_url` | `VARCHAR(1024)` | Link to official docs page |
| `announced_at` | `DATETIME(tz)` nullable | Date announced by provider |
| `effective_at` | `DATETIME(tz)` nullable | Date the change takes effect |
| `raw` | `TEXT` nullable | JSON blob of raw parsed data |
| `created_at` | `DATETIME(tz)` | Set at insert time (`datetime.now(UTC)`) |
| `fingerprint` | `VARCHAR(64)` UNIQUE | SHA-256 deduplication key |

**Indexes:** `provider`, `model`, `change_type`, `severity`, `created_at`, `(provider, severity)`, and a unique index on `fingerprint`.

---

## Deduplication

Every `ModelUpdateCreate` computes a **SHA-256 fingerprint** in `schemas.py`:

```python
compute_fingerprint(provider, change_type, model, effective_at, source_url, title)
```

Fields are normalised (lowercased / stripped), joined with `|`, then hashed. The fingerprint is stored as a `UNIQUE` column — duplicate `INSERT` attempts raise `IntegrityError`, which `crud.create_update` catches and returns `None` for. This means calling `POST /api/collect` repeatedly is always safe.

---

## Configuration

All settings are loaded once at import time by `config.py` into a module-level `settings` singleton. Values are read from environment variables or a `.env` file (via `pydantic-settings`). See the [root README](../README.md#configuration-reference) for the full list of variables.

---

## Logging

`main.py` configures `structlog` at startup with:
- **ConsoleRenderer** with colors for local development
- ISO 8601 timestamps
- Log level, logger name, and exception info attached to every record

Use Python's standard `logging.getLogger(__name__)` in any module — `structlog` intercepts and formats it automatically.

---

## Adding a new route

1. Add a handler function in `main.py`.
2. Use `DBDep` (`Annotated[Session, Depends(get_db)]`) for database access.
3. Add a corresponding schema in `schemas.py` if the request/response shape is new.
4. Add CRUD helpers in `crud.py` if new queries are needed.
