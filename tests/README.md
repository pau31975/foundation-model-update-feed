# `tests/` — Test Suite

This directory contains the pytest test suite for the LLM Provider Update Feed service. Tests cover deduplication logic, database persistence, fingerprint computation, and the HTML-parsing behaviour of each implemented collector.

---

## File map

| File | Role |
|---|---|
| `__init__.py` | Package marker, no logic |
| `test_dedupe.py` | All tests — dedupe, DB integration, collector parsing |

---

## Running tests

```bash
# With uv (recommended)
uv run pytest tests/ -v

# With make
make test

# With plain pytest (after activating venv)
pytest tests/ -v
```

---

## Test architecture

### In-memory database fixture

`test_dedupe.py` defines a `db_session` pytest fixture that:

1. Creates an **in-memory SQLite** engine (`sqlite:///:memory:`).
2. Runs `Base.metadata.create_all()` to create all tables.
3. Yields a `Session` to the test.
4. Calls `Base.metadata.drop_all()` after the test completes.

Each test that uses `db_session` gets a completely isolated database — no state leaks between tests.

### HTTP mocking

Collector tests use `unittest.mock.patch.object` to replace the collector's `_fetch` method with a function that returns fixture HTML strings directly, bypassing the network entirely. This makes the tests fast, deterministic, and runnable offline.

```python
with patch.object(collector, "_fetch", return_value=FIXTURE_HTML):
    items = collector._collect_deprecations()
```

---

## Test classes

### `TestFingerprintComputation`

Unit tests for the `compute_fingerprint` function in `app/schemas.py`.

| Test | What it verifies |
|---|---|
| `test_same_inputs_produce_same_fingerprint` | Fingerprint is deterministic — same inputs always produce the same hash |
| `test_different_model_produces_different_fingerprint` | Changing the `model` field changes the fingerprint |
| `test_different_provider_produces_different_fingerprint` | Changing `provider` changes the fingerprint |
| `test_none_model_handled_without_error` | `model=None` does not raise; result is a valid 64-char hex string |
| `test_fingerprint_property_on_schema` | `ModelUpdateCreate.fingerprint` property matches a direct call to `compute_fingerprint` with the same values |

---

### `TestDeduplicationViaDB`

Integration tests for the full insert-and-deduplicate path using a real (in-memory) SQLite database.

| Test | What it verifies |
|---|---|
| `test_first_insert_succeeds` | `crud.create_update` returns a `ModelUpdate` ORM object on the first insert |
| `test_duplicate_insert_returns_none` | Inserting the identical item a second time returns `None` (not an exception) |
| `test_different_items_both_stored` | Two items with different `model` / `title` fields both persist and receive distinct UUIDs |
| `test_fingerprint_exists_helper` | `crud.fingerprint_exists` returns `False` before insert and `True` after |

---

### `TestGeminiCollectorParsing`

Tests for `GeminiCollector`'s HTML parsing logic, using a minimal fixture HTML table.

**Fixture HTML** (`FIXTURE_HTML`):

A minimal `<table>` with one data row:
- Model: `gemini-1.0-pro`
- Deprecation date: `September 19, 2024`
- Shutdown date: `February 15, 2025`
- Replacement: `gemini-1.5-pro`

| Test | What it verifies |
|---|---|
| `test_parses_table_row` | Returns 1 item; `model="gemini-1.0-pro"`, `provider=Provider.google`, `change_type=RETIREMENT`, `severity=CRITICAL`, `effective_at` = 2025-02 |
| `test_returns_empty_when_fetch_fails` | When both `_fetch` and `_fetch_rss` return `None`/`[]`, `collect()` returns `[]` (no seed fallback) |

---

### `TestOpenAICollectorParsing`

Tests for `OpenAICollector`'s `<dl>` parsing strategy, using a minimal fixture HTML definition list.

**Fixture HTML** (`FIXTURE_DL_HTML`):

A `<dl>` with one `<dt>`/`<dd>` pair:
- Model: `gpt-4-0314`
- Description: `"Deprecated on April 5, 2024. Shutdown on June 6, 2024."`

| Test | What it verifies |
|---|---|
| `test_parses_definition_list` | Returns 1 item; `model="gpt-4-0314"`, `provider=Provider.openai`, `change_type=RETIREMENT` |
| `test_returns_empty_when_fetch_fails` | When both `_fetch` and `_fetch_rss` return `None`/`[]`, `collect()` returns `[]` (no seed fallback) |

---

## Coverage gaps and future tests

The following areas are not yet covered by tests:

| Area | Suggested tests |
|---|---|
| `GET /api/updates` route | FastAPI `TestClient` integration tests for filtering, pagination, cursor |
| `POST /api/updates` route | Valid create, duplicate 409, invalid body 422 |
| `POST /api/collect` route | End-to-end with mocked collectors |
| Gemini changelog parsing | `_collect_changelog()` with fixture HTML |
| OpenAI heading fallback | `_parse_headings()` strategy |
| `config.py` | Settings read from env vars and `.env` |
| `crud.list_updates` | Filtering by provider, severity, change_type, since, cursor |

---

## Dependencies

Test-only dependencies are declared in `pyproject.toml` under `[dependency-groups] dev`:

```toml
[dependency-groups]
dev = [
  "pytest>=8.0.0",
  "pytest-asyncio>=0.23.0",
]
```

Install them with `uv sync` (they are included in `uv.lock`).
