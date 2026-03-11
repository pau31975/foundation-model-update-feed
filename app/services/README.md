# `app/services/` — Service Layer

The `services/` sub-package contains business-logic orchestration that sits between the FastAPI route handlers in `main.py` and the low-level database operations in `crud.py`. It coordinates work that spans multiple modules or multiple steps.

---

## File map

| File | Role |
|---|---|
| `__init__.py` | Package marker, no logic |
| `collector_service.py` | Orchestrates all provider collectors and persists results |

---

## `collector_service.py`

### Purpose

`collector_service.py` is the single entry point for running the entire collector pipeline. It:

1. Iterates the registered collector classes in order.
2. Instantiates each one (which creates its HTTP client).
3. Calls `.collect()`, catches any unhandled exceptions, and logs them without aborting the rest of the pipeline.
4. Passes each returned `ModelUpdateCreate` item to `crud.create_update` for persistence.
5. Tracks how many items were added, skipped (duplicate), and which errors occurred.
6. Returns a `CollectResult` summary.

### Collector registry

```python
_ALL_COLLECTORS: list[type[BaseCollector]] = [
    GeminiCollector,
    OpenAICollector,
    AnthropicCollector,
    AzureCollector,
    AWSCollector,
]
```

This list is the **single place** where collectors are registered. Adding a new collector means appending its class here — no other changes are needed in this file.

### Function signature

```python
def run_all_collectors(db: Session) -> CollectResult:
```

| Parameter | Type | Description |
|---|---|---|
| `db` | `sqlalchemy.orm.Session` | Active database session, injected from `main.py` via FastAPI's `Depends` |

| Return field | Type | Description |
|---|---|---|
| `added` | `int` | Number of new items persisted |
| `skipped` | `int` | Number of items ignored as duplicates (same fingerprint already exists) |
| `errors` | `list[str]` | Error messages from any collector or persistence failure |

### Error isolation

Each collector is wrapped in a `try/except` block. If a collector raises an unhandled exception, the error is logged with full traceback (`logger.exception`) and appended to `errors`, but the remaining collectors continue to run. The same isolation applies to each individual `crud.create_update` call.

This means a broken network connection to one provider will not prevent data from the other providers from being collected.

### Execution flow

```
run_all_collectors(db)
      │
      ├─► GeminiCollector()
      │        └─ .collect() → [item1, item2, ...]
      │             ├─ crud.create_update(db, item1) → ModelUpdate (added)
      │             └─ crud.create_update(db, item2) → None (skipped, duplicate)
      │
      ├─► OpenAICollector()
      │        └─ .collect() → [item3, ...]
      │             └─ crud.create_update(db, item3) → ModelUpdate (added)
      │
      ├─► AnthropicCollector()
      │        └─ .collect() → [item4, ...]  (models page + changelog)
      │             └─ crud.create_update(db, item4) → ModelUpdate (added)
      │
      ├─► AzureCollector()
      │        └─ .collect() → [live items...] + _SEED_ENTRIES (always)
      │             └─ crud.create_update(db, item) → ModelUpdate or None (dup)
      │
      └─► AWSCollector()
               └─ .collect() → [live items...] + _SEED_ENTRIES (always)
                    └─ crud.create_update(db, item) → ModelUpdate or None (dup)

             Note: Azure and AWS always append _SEED_ENTRIES on top of
             live results. Fingerprint deduplication silently skips seeds
             that were already stored in a previous run.
                    │
                    ▼
           CollectResult(added=2, skipped=1, errors=[])
```

### Called from

`main.py`, `POST /api/collect` route:

```python
@app.post("/api/collect", response_model=CollectResult, tags=["ops"])
async def collect(db: DBDep) -> CollectResult:
    return run_all_collectors(db)
```

The client-side JavaScript in `app/static/app.js` calls this endpoint when the user clicks **Run collectors now** in the web UI.

---

## Adding a new service

To add other services (e.g. a notification service, a scheduled cleanup job), follow the same pattern:

1. Create a new file: `app/services/my_service.py`.
2. Define a function that accepts a `Session` (and any other dependencies) and returns a result schema.
3. Import and call it from the relevant route in `main.py`.

Keep service functions free of HTTP-layer concerns (requests, responses, status codes). They should operate on plain Python objects and database sessions only.
