"""Abstract base class for all provider collectors."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from app.config import settings
from app.schemas import ModelUpdateCreate

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Every provider collector inherits from this base.

    Subclasses must implement :meth:`collect` which returns a (possibly empty)
    list of :class:`~app.schemas.ModelUpdateCreate` objects ready for storage.
    """

    #: Short human-readable label used in logs and error messages.
    provider_name: str = "unknown"

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=settings.collector_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "llm-provider-update-feed/1.0 "
                    "(https://github.com/your-org/llm-provider-update-feed)"
                )
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @abstractmethod
    def collect(self) -> list[ModelUpdateCreate]:
        """Fetch and parse provider-specific sources.

        Returns a list of normalised :class:`~app.schemas.ModelUpdateCreate`
        objects.  Never raises – all exceptions should be caught internally and
        surfaced via logging.
        """

    # ------------------------------------------------------------------
    # Helpers available to subclasses
    # ------------------------------------------------------------------

    def _fetch(self, url: str) -> str | None:
        """GET *url* and return the response text, or None on failure."""
        for attempt in range(1, settings.collector_max_retries + 2):
            try:
                response = self._client.get(url)
                response.raise_for_status()
                return response.text
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "[%s] HTTP %s for %s (attempt %d)",
                    self.provider_name,
                    exc.response.status_code,
                    url,
                    attempt,
                )
            except httpx.RequestError as exc:
                logger.warning(
                    "[%s] Request error for %s: %s (attempt %d)",
                    self.provider_name,
                    url,
                    exc,
                    attempt,
                )
        return None

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
