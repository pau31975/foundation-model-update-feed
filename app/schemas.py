"""Pydantic schemas for request validation and API responses."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Provider(str, Enum):
    google = "google"
    openai = "openai"
    anthropic = "anthropic"
    azure = "azure"
    aws = "aws"


class ChangeType(str, Enum):
    NEW_MODEL = "NEW_MODEL"
    DEPRECATION_ANNOUNCED = "DEPRECATION_ANNOUNCED"
    RETIREMENT = "RETIREMENT"
    SHUTDOWN_DATE_CHANGED = "SHUTDOWN_DATE_CHANGED"
    CAPABILITY_CHANGED = "CAPABILITY_CHANGED"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def compute_fingerprint(
    provider: str,
    change_type: str,
    model: Optional[str],
    effective_at: Optional[datetime],
    source_url: str,
    title: str,
) -> str:
    """Return a SHA-256 hex digest that uniquely identifies an update event.

    Two events with identical (provider, change_type, model, effective_at,
    source_url, title) tuples are considered duplicates.
    """
    effective_str = effective_at.isoformat() if effective_at else ""
    raw = "|".join(
        [
            provider.lower(),
            change_type.upper(),
            (model or "").lower().strip(),
            effective_str,
            source_url.strip(),
            title.strip(),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Create / incoming schemas
# ---------------------------------------------------------------------------


class ModelUpdateCreate(BaseModel):
    """Schema used when creating a new feed item (via API or collectors)."""

    provider: Provider
    product: str = Field(..., max_length=64)
    model: Optional[str] = Field(None, max_length=128)
    change_type: ChangeType
    severity: Severity
    title: str = Field(..., max_length=256)
    summary: str
    source_url: str = Field(..., max_length=1024)
    announced_at: Optional[datetime] = None
    effective_at: Optional[datetime] = None
    raw: Optional[Any] = None

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Basic URL sanity check."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("source_url must start with http:// or https://")
        return v

    @property
    def fingerprint(self) -> str:
        """Compute the deduplication fingerprint for this item."""
        return compute_fingerprint(
            provider=self.provider.value,
            change_type=self.change_type.value,
            model=self.model,
            effective_at=self.effective_at,
            source_url=self.source_url,
            title=self.title,
        )

    def raw_json(self) -> Optional[str]:
        """Serialize ``raw`` to a JSON string (or None)."""
        import json

        if self.raw is None:
            return None
        return json.dumps(self.raw, default=str)


# ---------------------------------------------------------------------------
# Read / outgoing schemas
# ---------------------------------------------------------------------------


class ModelUpdateRead(BaseModel):
    """Schema returned by the REST API for a single feed item."""

    model_config = {"from_attributes": True}

    id: str
    provider: str
    product: str
    model: Optional[str]
    change_type: str
    severity: str
    title: str
    summary: str
    source_url: str
    announced_at: Optional[datetime]
    effective_at: Optional[datetime]
    created_at: datetime
    fingerprint: str


class FeedPage(BaseModel):
    """Paginated list of feed items."""

    items: List[ModelUpdateRead]
    total: int
    limit: int
    next_cursor: Optional[str] = None


# ---------------------------------------------------------------------------
# Query / filter schemas
# ---------------------------------------------------------------------------


class FeedQuery(BaseModel):
    """Query parameters for GET /api/updates."""

    provider: Optional[Provider] = None
    severity: Optional[Severity] = None
    change_type: Optional[ChangeType] = None
    since: Optional[datetime] = None
    limit: int = Field(50, ge=1, le=200)
    cursor: Optional[str] = None  # opaque: last seen created_at ISO string
    major_only: bool = False  # show only NEW_MODEL, RETIREMENT, DEPRECATION_ANNOUNCED with severity WARN/CRITICAL


# ---------------------------------------------------------------------------
# Response for /api/collect
# ---------------------------------------------------------------------------


class CollectResult(BaseModel):
    """Response body for POST /api/collect."""

    added: int
    skipped: int
    errors: List[str] = []
