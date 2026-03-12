# app/services/

Orchestration layer that coordinates all collectors and persists results to the database.

---

## `collector_service.py`

### `run_all_collectors(db: Session) -> CollectResult`

The single entry point called by `POST /api/collect`.

**Flow:**
1. Iterates over `_ALL_COLLECTORS` — the registry of all provider collector classes.
2. Instantiates each collector and calls `.collect()`.
3. Calls `crud.create_update()` for each returned item.
   - `None` return → duplicate fingerprint → increments `skipped`.
   - Successful insert → increments `added`.
4. Per-item and per-collector exceptions are caught, logged, and appended to `errors`.
5. Returns `CollectResult(added, skipped, errors)`.

### `_ALL_COLLECTORS`

```python
_ALL_COLLECTORS = [
    GeminiCollector,
    OpenAICollector,
    AnthropicCollector,
    AzureCollector,
    AWSCollector,
]
```

To register a new provider, append its collector class here.

---

## `CollectResult` schema

Defined in `app/schemas.py`. Returned as JSON by `POST /api/collect`.

| Field | Type | Description |
|-------|------|-------------|
| `added` | `int` | New events inserted |
| `skipped` | `int` | Duplicates ignored |
| `errors` | `list[str]` | Error messages from failed collectors or items |
