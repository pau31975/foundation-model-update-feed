# tests/

Unit tests for the application.

---

## `test_dedupe.py`

Tests for the SHA-256 fingerprint deduplication logic defined in `app/schemas.py`.

**What is tested:**
- Identical events produce the same fingerprint.
- Events differing in any key field (`provider`, `change_type`, `model`, `effective_at`, `source_url`, `title`) produce distinct fingerprints.
- Normalization — case differences and extra whitespace in field values are collapsed before hashing, so semantically equal events always match.

---

## Running Tests

```bash
make test
# or
uv run pytest tests/ -v
```

---

## Adding Tests

Place new test files in this directory following the `test_<module>.py` naming convention. pytest discovers them automatically.

For tests that require a database session, use SQLAlchemy's in-memory SQLite (`sqlite:///:memory:`) and call `init_db()` in a fixture to avoid touching the `data/` directory.
