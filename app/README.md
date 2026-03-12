# app/

The core application package. Built with FastAPI + SQLAlchemy (SQLite) + Pydantic v2.

---

## Files

### `main.py`
Application entry point. Defines all HTTP routes, wires dependencies, and configures structured logging via `structlog`.

- **Lifespan:** calls `init_db()` on startup to create DB tables.
- **`DBDep`** — reusable `Annotated[Session, Depends(get_db)]` dependency.
- Mounts `app/static/` at `/static` and uses Jinja2 for the `templates/` directory.

**Routes:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/` | Server-rendered feed UI (major events only) |
| `GET` | `/api/updates` | Paginated JSON feed with filters |
| `POST` | `/api/updates` | Manually create a feed item |
| `POST` | `/api/collect` | Trigger all collectors |

---

### `config.py`
All settings loaded from environment variables or `.env` via `pydantic-settings` (`BaseSettings`).

Key groups:
- **Server:** `HOST`, `PORT`, `RELOAD`, `LOG_LEVEL`
- **Database:** `DATABASE_URL` (default: `sqlite:///./data/updates.db`)
- **Collector tuning:** `COLLECTOR_TIMEOUT_SECONDS`, `COLLECTOR_MAX_RETRIES`
- **Pagination:** `DEFAULT_PAGE_LIMIT`, `MAX_PAGE_LIMIT`
- **Source URLs:** `GEMINI_SOURCE_URLS`, `OPENAI_SOURCE_URLS`, `ANTHROPIC_SOURCE_URLS`, `AZURE_SOURCE_URLS`, `AWS_SOURCE_URLS` — each overridable as a JSON array env var
- **RSS feeds:** `OPENAI_RSS_URL`, `GOOGLE_RSS_URL`, `AWS_RSS_URL`

---

### `models.py`
SQLAlchemy ORM definition for the `model_updates` table.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `String(36)` | UUID primary key |
| `provider` | `String(32)` | Indexed |
| `product` | `String(64)` | e.g. `gemini_api`, `aws_bedrock` |
| `model` | `String(128)` | Nullable, indexed |
| `change_type` | `String(64)` | Indexed |
| `severity` | `String(16)` | Indexed |
| `title` | `String(256)` | |
| `summary` | `Text` | |
| `source_url` | `String(1024)` | |
| `announced_at` | `DateTime(tz)` | Nullable |
| `effective_at` | `DateTime(tz)` | Nullable |
| `raw` | `Text` | JSON-serialized raw collector output |
| `created_at` | `DateTime(tz)` | Auto-set to UTC now |
| `fingerprint` | `String(64)` | SHA-256, unique — enforces deduplication |

Composite indexes on `(provider, severity)` and `(created_at)`.

---

### `schemas.py`
Pydantic v2 schemas and enums for validation, serialization, and fingerprinting.

**Enums:**
- `Provider`: `google`, `openai`, `anthropic`, `azure`, `aws`
- `ChangeType`: `NEW_MODEL`, `DEPRECATION_ANNOUNCED`, `RETIREMENT`, `SHUTDOWN_DATE_CHANGED`, `CAPABILITY_CHANGED`
- `Severity`: `INFO`, `WARN`, `CRITICAL`

**Key schemas:**
- `ModelUpdateCreate` — inbound data; validates `source_url` (must be `http(s)://`); exposes `.fingerprint` property and `.raw_json()` serializer.
- `ModelUpdateRead` — outbound ORM-mapped schema.
- `FeedPage` — wraps `items`, `total`, `limit`, `next_cursor`.
- `FeedQuery` — internal query spec with all filter fields plus `major_only`.
- `CollectResult` — `{added, skipped, errors}` response for `POST /api/collect`.

**`compute_fingerprint()`** hashes `(provider, change_type, model, effective_at, source_url, title)` via SHA-256 after normalizing case and whitespace.

---

### `crud.py`
Database access layer. All queries use SQLAlchemy 2.0 `select()` style.

| Function | Description |
|----------|-------------|
| `create_update(db, item)` | Insert a row; returns `None` on duplicate fingerprint (`IntegrityError`) |
| `get_update(db, update_id)` | Fetch by UUID |
| `list_updates(db, query)` | Filtered + paginated query; ordered by `announced_at DESC`, then `created_at DESC` |
| `fingerprint_exists(db, fp)` | Lightweight existence check |

Cursor decoding: `next_cursor` is an ISO timestamp compared against `created_at`.

---

### `db.py`
SQLAlchemy engine, session factory, and shared `DeclarativeBase`.

- Creates the `data/` directory if absent (SQLite).
- `engine` — `check_same_thread=False` for FastAPI compatibility.
- `SessionLocal` — `autocommit=False, autoflush=False`.
- `get_db()` — FastAPI dependency generator (yield + `finally: db.close()`).
- `init_db()` — registers ORM classes and calls `Base.metadata.create_all()`.

---

## Sub-packages

| Package | Description |
|---------|-------------|
| [`collectors/`](collectors/README.md) | Per-provider scrapers |
| [`services/`](services/README.md) | Collector orchestration |
| [`templates/`](templates/README.md) | Jinja2 web UI template |
| [`static/`](static/README.md) | Frontend JS and CSS |
