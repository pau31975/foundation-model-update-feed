"""Tests for deduplication logic and basic collector parsing."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.schemas import (
    ChangeType,
    ModelUpdateCreate,
    Provider,
    Severity,
    compute_fingerprint,
)


# ---------------------------------------------------------------------------
# Fingerprint / deduplication unit tests
# ---------------------------------------------------------------------------


def _make_item(**overrides) -> ModelUpdateCreate:
    """Create a minimal valid ModelUpdateCreate with sensible defaults."""
    base = dict(
        provider=Provider.openai,
        product="openai_api",
        model="gpt-4-0314",
        change_type=ChangeType.RETIREMENT,
        severity=Severity.CRITICAL,
        title="GPT-4 0314 shutdown",
        summary="This model was shut down.",
        source_url="https://platform.openai.com/docs/deprecations",
        announced_at=datetime(2024, 4, 5, tzinfo=timezone.utc),
        effective_at=datetime(2024, 6, 6, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return ModelUpdateCreate(**base)


class TestFingerprintComputation:
    """compute_fingerprint should be deterministic and sensitive to key fields."""

    def test_same_inputs_produce_same_fingerprint(self) -> None:
        fp1 = compute_fingerprint(
            provider="openai",
            change_type="RETIREMENT",
            model="gpt-4-0314",
            effective_at=datetime(2024, 6, 6, tzinfo=timezone.utc),
            source_url="https://platform.openai.com/docs/deprecations",
            title="GPT-4 0314 shutdown",
        )
        fp2 = compute_fingerprint(
            provider="openai",
            change_type="RETIREMENT",
            model="gpt-4-0314",
            effective_at=datetime(2024, 6, 6, tzinfo=timezone.utc),
            source_url="https://platform.openai.com/docs/deprecations",
            title="GPT-4 0314 shutdown",
        )
        assert fp1 == fp2

    def test_different_model_produces_different_fingerprint(self) -> None:
        fp1 = compute_fingerprint(
            provider="openai",
            change_type="RETIREMENT",
            model="gpt-4-0314",
            effective_at=None,
            source_url="https://example.com",
            title="Some title",
        )
        fp2 = compute_fingerprint(
            provider="openai",
            change_type="RETIREMENT",
            model="gpt-4-turbo",
            effective_at=None,
            source_url="https://example.com",
            title="Some title",
        )
        assert fp1 != fp2

    def test_different_provider_produces_different_fingerprint(self) -> None:
        kwargs = dict(
            change_type="RETIREMENT",
            model="some-model",
            effective_at=None,
            source_url="https://example.com",
            title="Same title",
        )
        fp_openai = compute_fingerprint(provider="openai", **kwargs)
        fp_google = compute_fingerprint(provider="google", **kwargs)
        assert fp_openai != fp_google

    def test_none_model_handled_without_error(self) -> None:
        fp = compute_fingerprint(
            provider="google",
            change_type="NEW_MODEL",
            model=None,
            effective_at=None,
            source_url="https://ai.google.dev",
            title="New model",
        )
        assert isinstance(fp, str)
        assert len(fp) == 64  # SHA-256 hex length

    def test_fingerprint_property_on_schema(self) -> None:
        item = _make_item()
        expected = compute_fingerprint(
            provider=item.provider.value,
            change_type=item.change_type.value,
            model=item.model,
            effective_at=item.effective_at,
            source_url=item.source_url,
            title=item.title,
        )
        assert item.fingerprint == expected


class TestDeduplicationViaDB:
    """Integration-style dedupe tests using an in-memory SQLite database."""

    @pytest.fixture()
    def db_session(self):
        """Provide a fresh in-memory SQLite session for each test."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.db import Base
        import app.models  # noqa: F401 – registers ORM classes

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()
        Base.metadata.drop_all(engine)

    def test_first_insert_succeeds(self, db_session) -> None:
        from app import crud

        item = _make_item()
        result = crud.create_update(db_session, item)
        assert result is not None
        assert result.provider == "openai"
        assert result.model == "gpt-4-0314"

    def test_duplicate_insert_returns_none(self, db_session) -> None:
        from app import crud

        item = _make_item()
        first = crud.create_update(db_session, item)
        second = crud.create_update(db_session, item)

        assert first is not None
        assert second is None  # duplicate detected

    def test_different_items_both_stored(self, db_session) -> None:
        from app import crud

        item_a = _make_item(model="gpt-4-0314")
        item_b = _make_item(model="gpt-3.5-turbo-0301", title="GPT-3.5 0301 shutdown")

        result_a = crud.create_update(db_session, item_a)
        result_b = crud.create_update(db_session, item_b)

        assert result_a is not None
        assert result_b is not None
        assert result_a.id != result_b.id

    def test_fingerprint_exists_helper(self, db_session) -> None:
        from app import crud

        item = _make_item()
        assert not crud.fingerprint_exists(db_session, item.fingerprint)
        crud.create_update(db_session, item)
        assert crud.fingerprint_exists(db_session, item.fingerprint)


# ---------------------------------------------------------------------------
# Collector parsing – smoke tests with mocked HTTP responses
# ---------------------------------------------------------------------------


class TestGeminiCollectorParsing:
    """Smoke-test the Gemini collector against a minimal HTML fixture."""

    FIXTURE_HTML = """
    <html><body>
    <table>
      <thead>
        <tr><th>Model</th><th>Deprecation Date</th><th>Shutdown Date</th><th>Replacement</th></tr>
      </thead>
      <tbody>
        <tr>
          <td>gemini-1.0-pro</td>
          <td>September 19, 2024</td>
          <td>February 15, 2025</td>
          <td>gemini-1.5-pro</td>
        </tr>
      </tbody>
    </table>
    </body></html>
    """

    def test_parses_table_row(self) -> None:
        from app.collectors.gemini import GeminiCollector

        collector = GeminiCollector()

        with patch.object(collector, "_fetch", return_value=self.FIXTURE_HTML):
            items = collector._collect_deprecations()

        assert len(items) == 1
        item = items[0]
        assert item.model == "gemini-1.0-pro"
        assert item.provider == Provider.google
        assert item.change_type == ChangeType.RETIREMENT
        assert item.severity == Severity.CRITICAL
        assert item.effective_at is not None
        assert item.effective_at.year == 2025
        assert item.effective_at.month == 2

    def test_returns_empty_when_fetch_fails(self) -> None:
        from app.collectors.gemini import GeminiCollector

        collector = GeminiCollector()

        with patch.object(collector, "_fetch", return_value=None), \
             patch.object(collector, "_fetch_rss", return_value=[]):
            items = collector.collect()

        assert items == []


class TestOpenAICollectorParsing:
    """Smoke-test the OpenAI collector against a minimal HTML fixture."""

    FIXTURE_DL_HTML = """
    <html><body>
    <dl>
      <dt>gpt-4-0314</dt>
      <dd>Deprecated on April 5, 2024. Shutdown on June 6, 2024. Use gpt-4-turbo instead.</dd>
    </dl>
    </body></html>
    """

    def test_parses_definition_list(self) -> None:
        from app.collectors.openai import OpenAICollector

        collector = OpenAICollector()

        with patch.object(collector, "_fetch", return_value=self.FIXTURE_DL_HTML):
            items = collector._collect_deprecations()

        assert len(items) == 1
        item = items[0]
        assert item.model == "gpt-4-0314"
        assert item.provider == Provider.openai
        assert item.change_type == ChangeType.RETIREMENT

    def test_returns_empty_when_fetch_fails(self) -> None:
        from app.collectors.openai import OpenAICollector

        collector = OpenAICollector()

        with patch.object(collector, "_fetch", return_value=None), \
             patch.object(collector, "_fetch_rss", return_value=[]):
            items = collector.collect()

        assert items == []
