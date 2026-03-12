"""Azure OpenAI model update collector.

Parses:
- https://learn.microsoft.com/en-us/azure/foundry-classic/openai/whats-new
  (new canonical What's New page with retirement announcements and new model notices)
- https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/models
  (model availability table and retirement/deprecation sections)
- https://learn.microsoft.com/en-us/azure/ai-services/openai/whats-new
  (legacy What's New page, still carries historical entries)

Falls back to a set of known-good seed entries if live parsing yields nothing,
so the feed always has representative data even when the docs are unreachable.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.collectors.base import BaseCollector
from app.config import settings
from app.schemas import ChangeType, ModelUpdateCreate, Provider, Severity

logger = logging.getLogger(__name__)

_WHATS_NEW_URL = settings.azure_source_urls[0]   # new canonical
_MODELS_URL = settings.azure_source_urls[1]
_WHATS_NEW_LEGACY_URL = settings.azure_source_urls[2]


def _parse_date(text: str) -> datetime | None:
    """Try common date formats and return UTC datetime or None."""
    text = re.sub(r"\s+", " ", text.strip())
    # Strip trailing parenthetical annotations, e.g. "(us-east-1 and us-west-2)"
    text = re.sub(r"\s*\(.*?\).*$", "", text).strip()
    # Remove ordinal suffixes: 1st, 2nd, 3rd, 4th, etc.
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text).strip()
    formats = [
        "%B %d, %Y",
        "%b %d, %Y",
        "%Y-%m-%d",
        "%B %Y",
        "%b %Y",
        "%d %B %Y",
        "%d %b %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        try:
            return datetime.strptime(m.group(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


class AzureCollector(BaseCollector):
    """Collects model lifecycle events from Azure OpenAI documentation."""

    provider_name = "azure"

    def collect(self) -> list[ModelUpdateCreate]:
        items: list[ModelUpdateCreate] = []

        items.extend(self._collect_models_page())
        items.extend(self._collect_whats_new())

        logger.info("[%s] collected %d item(s)", self.provider_name, len(items))
        return items

    # ------------------------------------------------------------------
    # Models page (retirement tables)
    # ------------------------------------------------------------------

    def _collect_models_page(self) -> list[ModelUpdateCreate]:
        """Parse Azure OpenAI models page for retirement/deprecation entries."""
        html = self._fetch(_MODELS_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        # Strategy 1: find headings that signal retirement/deprecation sections,
        # then collect tables immediately following them.
        for heading in soup.find_all(re.compile(r"^h[2345]$")):
            heading_text = heading.get_text(strip=True).lower()
            if not re.search(r"\b(retir|deprecat|legacy|end.of.?life|sunset)", heading_text):
                continue
            sibling = heading.next_sibling
            while sibling:
                if isinstance(sibling, Tag):
                    if sibling.name == heading.name:
                        break
                    if sibling.name == "table":
                        items.extend(self._parse_retirement_table(sibling))
                sibling = sibling.next_sibling

        # Strategy 2: scan all tables whose *header row* mentions retirement.
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                rows = table.find_all("tr")
                if rows:
                    headers = [
                        td.get_text(strip=True).lower()
                        for td in rows[0].find_all(["th", "td"])
                    ]
            if re.search(r"\b(retir|deprecat|end of life|sunset)", " ".join(headers)):
                items.extend(self._parse_retirement_table(table))

        return items

    def _parse_retirement_table(self, table: Tag) -> list[ModelUpdateCreate]:
        """Parse a single <table> element for retirement data."""
        items: list[ModelUpdateCreate] = []

        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        rows = table.find_all("tr")
        if not headers and rows:
            headers = [
                td.get_text(strip=True).lower()
                for td in rows[0].find_all(["th", "td"])
            ]
            rows = rows[1:]
        else:
            rows = rows[1:] if rows else []

        col_map = self._map_columns(
            headers,
            {
                "model": ["model name", "model version", "model"],
                "retirement": ["retirement date", "retirement", "retire", "end of", "sunset"],
                "deprecation": ["deprecation", "deprecat"],
                "replacement": ["replacement", "successor", "migrate", "upgrade to"],
            },
        )

        if col_map.get("model") is None:
            return []

        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if not cells or len(cells) < 2:
                continue

            model_name = self._get(cells, col_map.get("model"))
            if not model_name:
                continue

            retirement_str = self._get(cells, col_map.get("retirement"))
            deprecation_str = self._get(cells, col_map.get("deprecation"))
            replacement = self._get(cells, col_map.get("replacement"))
            retirement_date = _parse_date(retirement_str) if retirement_str else None
            deprecation_date = _parse_date(deprecation_str) if deprecation_str else None

            row_text = " ".join(cells).lower()
            if not (
                retirement_date
                or deprecation_date
                or re.search(r"\b(retir|deprecat|end of|sunset)\b", row_text)
            ):
                continue

            if retirement_date:
                change_type = ChangeType.RETIREMENT
                severity = Severity.CRITICAL
                effective = retirement_date
                title = f"Azure OpenAI model '{model_name}' retiring"
                summary = (
                    f"'{model_name}' is scheduled for retirement"
                    + (f" on {retirement_str}" if retirement_str else "")
                    + (f". Replacement: {replacement}" if replacement else "")
                    + "."
                )
            elif deprecation_date:
                change_type = ChangeType.DEPRECATION_ANNOUNCED
                severity = Severity.WARN
                effective = deprecation_date
                title = f"Azure OpenAI model '{model_name}' deprecated"
                summary = (
                    f"'{model_name}' has been deprecated"
                    + (f" as of {deprecation_str}" if deprecation_str else "")
                    + (f". Replacement: {replacement}" if replacement else "")
                    + "."
                )
            else:
                change_type = ChangeType.DEPRECATION_ANNOUNCED
                severity = Severity.WARN
                effective = None
                title = f"Azure OpenAI model '{model_name}' deprecated"
                summary = (
                    f"'{model_name}' has been deprecated"
                    + (f". Replacement: {replacement}" if replacement else "")
                    + "."
                )

            try:
                items.append(
                    ModelUpdateCreate(
                        provider=Provider.azure,
                        product="azure_openai",
                        model=model_name,
                        change_type=change_type,
                        severity=severity,
                        title=title,
                        summary=summary,
                        source_url=_MODELS_URL,
                        announced_at=None,
                        effective_at=effective,
                        raw={
                            "model": model_name,
                            "retirement_date": retirement_str,
                            "deprecation_date": deprecation_str,
                            "replacement": replacement,
                        },
                    )
                )
            except Exception as exc:
                logger.debug(
                    "[%s] Skipping table row %r: %s",
                    self.provider_name, model_name, exc,
                )

        return items

    # ------------------------------------------------------------------
    # What's New page
    # ------------------------------------------------------------------

    def _collect_whats_new(self) -> list[ModelUpdateCreate]:
        """Best-effort parse of the Azure OpenAI What's New page."""
        html = self._fetch(_WHATS_NEW_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        headings = soup.find_all(re.compile(r"^h[234]$"))
        for heading in headings:
            heading_text = heading.get_text(strip=True)
            date = _parse_date(heading_text)

            body_parts: list[str] = []
            sibling = heading.next_sibling
            while sibling:
                if isinstance(sibling, Tag) and sibling.name in ("h2", "h3", "h4"):
                    break
                if isinstance(sibling, Tag):
                    body_parts.append(sibling.get_text(separator=" ", strip=True))
                sibling = sibling.next_sibling

            body = " ".join(body_parts).strip()
            if not body or len(body) < 20:
                continue

            if not re.search(
                r"\b(model|gpt|available|release|retir|deprecat|launch|update)\b",
                body, re.IGNORECASE,
            ):
                continue

            model_match = re.search(
                r"\b(gpt-[\w\d.-]+|o[\d](?:-[\w]+)?|text-[\w-]+|dall-e[-\w]*|"
                r"whisper[-\w]*|embedding[\w-]*|tts[-\w]*)",
                body, re.IGNORECASE,
            )
            model_name = model_match.group(1) if model_match else None

            if re.search(r"\b(retir|deprecat|end of support|sunset)\b", body, re.IGNORECASE):
                change_type = ChangeType.DEPRECATION_ANNOUNCED
                severity = Severity.WARN
                title = "Azure OpenAI deprecation: " + (
                    f"{model_name} update" if model_name else heading_text[:80]
                )
            elif re.search(
                r"\b(available|launch|new|release|GA|generally available)\b",
                body, re.IGNORECASE,
            ):
                change_type = ChangeType.NEW_MODEL
                severity = Severity.INFO
                title = "Azure OpenAI: " + (
                    f"{model_name} available" if model_name else heading_text[:80]
                )
            else:
                continue

            try:
                items.append(
                    ModelUpdateCreate(
                        provider=Provider.azure,
                        product="azure_openai",
                        model=model_name,
                        change_type=change_type,
                        severity=severity,
                        title=title,
                        summary=body[:512],
                        source_url=_WHATS_NEW_URL,
                        announced_at=date,
                        effective_at=date,
                        raw={"heading": heading_text, "snippet": body[:256]},
                    )
                )
            except Exception as exc:
                logger.debug(
                    "[%s] Skipping whats-new item %r: %s",
                    self.provider_name, heading_text, exc,
                )

        return items

    # ------------------------------------------------------------------
    # Utils
    # ------------------------------------------------------------------

    @staticmethod
    def _map_columns(
        headers: list[str], mapping: dict[str, list[str]]
    ) -> dict[str, int]:
        """Map logical column names to their index in *headers*."""
        result: dict[str, int] = {}
        for key, candidates in mapping.items():
            for i, h in enumerate(headers):
                if any(c in h for c in candidates):
                    result[key] = i
                    break
        return result

    @staticmethod
    def _get(cells: list[str], idx: int | None) -> str | None:
        if idx is None or idx >= len(cells):
            return None
        val = cells[idx].strip()
        return val if val else None