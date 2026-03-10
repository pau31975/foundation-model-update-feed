"""Orchestrates all provider collectors and persists new items to the DB."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app import crud
from app.collectors.anthropic import AnthropicCollector
from app.collectors.aws import AWSCollector
from app.collectors.azure import AzureCollector
from app.collectors.base import BaseCollector
from app.collectors.gemini import GeminiCollector
from app.collectors.openai import OpenAICollector
from app.schemas import CollectResult

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Registry of all active collectors.  Add new ones here.
_ALL_COLLECTORS: list[type[BaseCollector]] = [
    GeminiCollector,
    OpenAICollector,
    AnthropicCollector,
    AzureCollector,
    AWSCollector,
]


def run_all_collectors(db: Session) -> CollectResult:
    """Instantiate every collector, run it, and persist deduplicated results.

    Returns a :class:`~app.schemas.CollectResult` summarising how many items
    were added vs skipped (duplicates) and any per-collector error messages.
    """
    total_added = 0
    total_skipped = 0
    errors: list[str] = []

    for collector_cls in _ALL_COLLECTORS:
        collector = collector_cls()
        provider = collector.provider_name
        try:
            items = collector.collect()
        except Exception as exc:
            msg = f"[{provider}] Unexpected error during collection: {exc}"
            logger.exception(msg)
            errors.append(msg)
            continue

        for item in items:
            try:
                result = crud.create_update(db, item)
                if result is None:
                    total_skipped += 1
                    logger.debug(
                        "[%s] Duplicate fingerprint, skipping: %s",
                        provider,
                        item.fingerprint,
                    )
                else:
                    total_added += 1
                    logger.info(
                        "[%s] Stored new item: %r (id=%s)",
                        provider,
                        item.title,
                        result.id,
                    )
            except Exception as exc:
                msg = f"[{provider}] Failed to persist item {item.title!r}: {exc}"
                logger.exception(msg)
                errors.append(msg)

    logger.info(
        "Collection complete: added=%d  skipped=%d  errors=%d",
        total_added,
        total_skipped,
        len(errors),
    )
    return CollectResult(added=total_added, skipped=total_skipped, errors=errors)
