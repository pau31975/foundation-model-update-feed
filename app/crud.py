"""CRUD operations for the model_updates table."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ModelUpdate
from app.schemas import FeedQuery, ModelUpdateCreate


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_update(db: Session, item: ModelUpdateCreate) -> ModelUpdate | None:
    """Insert a new ModelUpdate row.

    Returns the created row, or ``None`` if a duplicate fingerprint exists.
    """
    db_obj = ModelUpdate(
        provider=item.provider.value,
        product=item.product,
        model=item.model,
        change_type=item.change_type.value,
        severity=item.severity.value,
        title=item.title,
        summary=item.summary,
        source_url=item.source_url,
        announced_at=item.announced_at,
        effective_at=item.effective_at,
        raw=item.raw_json(),
        fingerprint=item.fingerprint,
    )
    db.add(db_obj)
    try:
        db.commit()
        db.refresh(db_obj)
        return db_obj
    except IntegrityError:
        db.rollback()
        return None  # duplicate


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get_update(db: Session, update_id: str) -> ModelUpdate | None:
    """Fetch a single ModelUpdate by its UUID string."""
    return db.get(ModelUpdate, update_id)


def list_updates(db: Session, query: FeedQuery) -> tuple[list[ModelUpdate], int]:
    """Return a filtered, paginated list of updates plus the total count.

    Returns ``(rows, total)``.  Pagination uses cursor-based approach where
    the cursor encodes the ``created_at`` of the last seen item.
    """
    base_stmt = select(ModelUpdate)
    count_stmt = select(func.count()).select_from(ModelUpdate)

    filters = []
    if query.provider is not None:
        filters.append(ModelUpdate.provider == query.provider.value)
    if query.severity is not None:
        filters.append(ModelUpdate.severity == query.severity.value)
    if query.change_type is not None:
        filters.append(ModelUpdate.change_type == query.change_type.value)
    if query.since is not None:
        filters.append(ModelUpdate.created_at >= query.since)
    if query.cursor is not None:
        try:
            cursor_dt = datetime.fromisoformat(query.cursor)
            filters.append(ModelUpdate.created_at < cursor_dt)
        except ValueError:
            pass  # ignore malformed cursor
    if query.major_only:
        filters.append(
            ModelUpdate.change_type.in_(["NEW_MODEL", "RETIREMENT", "DEPRECATION_ANNOUNCED"])
        )

    if filters:
        for f in filters:
            base_stmt = base_stmt.where(f)
            count_stmt = count_stmt.where(f)

    total: int = db.execute(count_stmt).scalar_one()

    # Use the later of announced_at / effective_at so that items from the
    # deprecations table (announced_at=None but a concrete effective_at) still
    # surface in a sensible position.
    sort_date = func.coalesce(ModelUpdate.announced_at, ModelUpdate.effective_at)
    stmt = base_stmt.order_by(
        sort_date.desc().nulls_last(),
        ModelUpdate.created_at.desc(),
    ).limit(query.limit)
    rows = list(db.execute(stmt).scalars().all())
    return rows, total


def fingerprint_exists(db: Session, fingerprint: str) -> bool:
    """Return True if a row with the given fingerprint already exists."""
    stmt = select(func.count()).select_from(ModelUpdate).where(
        ModelUpdate.fingerprint == fingerprint
    )
    return db.execute(stmt).scalar_one() > 0
