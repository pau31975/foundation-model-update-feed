"""SQLAlchemy ORM models for the llm-provider-update-feed service."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ModelUpdate(Base):
    """Stores a single model-lifecycle event from any LLM provider."""

    __tablename__ = "model_updates"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    product: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    change_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    announced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    effective_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # Deduplication fingerprint (SHA-256 hex digest)
    fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_model_updates_fingerprint"),
        Index("ix_model_updates_provider_severity", "provider", "severity"),
        Index("ix_model_updates_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ModelUpdate id={self.id!r} provider={self.provider!r} "
            f"model={self.model!r} change_type={self.change_type!r}>"
        )
