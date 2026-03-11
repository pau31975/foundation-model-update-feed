"""Abstract base class for all provider collectors."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import timezone
from email.utils import parsedate_to_datetime

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

    def _fetch_rss(self, url: str) -> list[dict]:
        """Fetch an RSS feed and return parsed entry dicts.

        Each dict has keys: ``title``, ``link``, ``description``, and
        ``pub_date`` (a UTC :class:`datetime` or ``None``).
        Returns an empty list on any failure.
        """
        raw = self._fetch(url)
        if not raw:
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            logger.warning("[%s] RSS parse error for %s: %s", self.provider_name, url, exc)
            return []

        entries: list[dict] = []
        for item in root.findall(".//item"):
            pub_str = item.findtext("pubDate")
            pub_date = None
            if pub_str:
                try:
                    pub_date = parsedate_to_datetime(pub_str).astimezone(timezone.utc)
                except Exception:
                    pass
            entries.append(
                {
                    "title": item.findtext("title") or "",
                    "link": item.findtext("link") or "",
                    "description": item.findtext("description") or "",
                    "pub_date": pub_date,
                }
            )
        return entries

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
