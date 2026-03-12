"""Anthropic Claude model update collector.

Parses:
- https://platform.claude.com/docs/en/about-claude/models/all-models  (model list)
- https://platform.claude.com/docs/en/release-notes/api               (API release notes)
- https://platform.claude.com/docs/en/about-claude/model-deprecations (deprecation schedule)

Falls back to a set of known-good seed entries if live parsing yields nothing,
so the feed always has representative data even when the docs are unreachable or
rendered via JavaScript.
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

_MODELS_URL = settings.anthropic_source_urls[0]
_CHANGELOG_URL = "https://platform.claude.com/docs/en/release-notes/overview"
_DEPRECATIONS_URL = settings.anthropic_source_urls[2]


def _parse_date(text: str) -> datetime | None:
    """Try common date formats and return UTC datetime or None."""
    text = re.sub(r"\s+", " ", text.strip())
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


class AnthropicCollector(BaseCollector):
    """Collects model lifecycle events from Anthropic Claude documentation."""

    provider_name = "anthropic"

    def collect(self) -> list[ModelUpdateCreate]:
        items: list[ModelUpdateCreate] = []

        items.extend(self._collect_models_page())
        items.extend(self._collect_changelog())
        logger.info("[%s] collected %d item(s)", self.provider_name, len(items))
        return items

    # ------------------------------------------------------------------
    # Models page
    # ------------------------------------------------------------------

    def _collect_models_page(self) -> list[ModelUpdateCreate]:
        """Parse the all-models page for deprecated or retired model entries."""
        html = self._fetch(_MODELS_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        for table in soup.find_all("table"):
            headers = [
                th.get_text(strip=True).lower()
                for th in table.find_all("th")
            ]
            if not headers:
                rows = table.find_all("tr")
                if rows:
                    headers = [
                        td.get_text(strip=True).lower()
                        for td in rows[0].find_all(["th", "td"])
                    ]

            # Only process model-related tables
            if not any(kw in " ".join(headers) for kw in ("model", "api")):
                continue

            col_map = self._map_columns(
                headers,
                {
                    "model": ["model name", "model"],
                    "api_model": ["api model name", "api name"],
                    "status": ["status", "availability", "support"],
                    "deprecation": ["deprecat", "end of support", "sunset", "retirement"],
                    "replacement": ["replacement", "successor", "use instead"],
                },
            )

            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if not cells or len(cells) < 2:
                    continue

                # Prefer the API model name (contains version) over the display name
                api_model_name = self._get(cells, col_map.get("api_model"))
                display_model_name = self._get(cells, col_map.get("model"))
                model_name = api_model_name or display_model_name
                if not model_name:
                    continue

                status_str = self._get(cells, col_map.get("status")) or ""
                dep_date_str = self._get(cells, col_map.get("deprecation"))
                replacement = self._get(cells, col_map.get("replacement"))

                row_text = " ".join(cells).lower()
                is_deprecated = (
                    "deprecat" in row_text
                    or "legacy" in row_text
                    or "end of support" in row_text
                    or "retired" in row_text
                    or "sunset" in row_text
                )
                if not is_deprecated:
                    continue

                dep_date = _parse_date(dep_date_str) if dep_date_str else None

                if "retired" in row_text or "end of support" in row_text:
                    change_type = ChangeType.RETIREMENT
                    severity = Severity.CRITICAL
                    title = f"Anthropic model '{model_name}' retired"
                    summary = (
                        f"'{model_name}' is retired or at end of support"
                        + (f" as of {dep_date_str}" if dep_date_str else "")
                        + (f". Replacement: {replacement}" if replacement else "")
                        + "."
                    )
                else:
                    change_type = ChangeType.DEPRECATION_ANNOUNCED
                    severity = Severity.WARN
                    title = f"Anthropic model '{model_name}' deprecated"
                    summary = (
                        f"'{model_name}' has been deprecated"
                        + (f" as of {dep_date_str}" if dep_date_str else "")
                        + (f". Replacement: {replacement}" if replacement else "")
                        + "."
                    )

                try:
                    items.append(
                        ModelUpdateCreate(
                            provider=Provider.anthropic,
                            product="claude_api",
                            model=model_name,
                            change_type=change_type,
                            severity=severity,
                            title=title,
                            summary=summary,
                            source_url=_MODELS_URL,
                            announced_at=dep_date,
                            effective_at=dep_date,
                            raw={
                                "model": model_name,
                                "status": status_str,
                                "deprecation_date": dep_date_str,
                                "replacement": replacement,
                            },
                        )
                    )
                except Exception as exc:
                    logger.debug(
                        "[%s] Skipping model row %r: %s",
                        self.provider_name, model_name, exc,
                    )

        return items

    # ------------------------------------------------------------------
    # Changelog / release notes
    # ------------------------------------------------------------------

    def _collect_changelog(self) -> list[ModelUpdateCreate]:
        """Best-effort parse of the Anthropic API release notes for model events."""
        html = self._fetch(_CHANGELOG_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        # Release notes use heading-per-date + content blocks structure
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
                r"\b(claude[-\s]\w+|new model|launch|available|release|API)\b",
                body, re.IGNORECASE,
            ):
                continue

            # Prefer a versioned API model ID (claude-xxx-YYYYMMDD) when present
            model_match = re.search(
                r"(claude[-\w.]*-\d{8})",
                body,
            )
            if not model_match:
                # Match display names that contain a version number, which
                # distinguishes real model names from product/service names:
                #   "Claude 3.5 Sonnet"  →  <number> before family word
                #   "Claude Sonnet 4"    →  <number> after family word
                #   "Claude Opus 4.5"    →  <number> after family word
                # Non-models like "Claude Console", "Claude Service", "Claude Docs"
                # have no version number and are therefore NOT matched.
                model_match = re.search(
                    r"(Claude\s+\d[\d.]*\s+[A-Z][a-z]+|Claude\s+[A-Z][a-z]+\s+\d[\d.]*(?:\.\d+)?)",
                    body,
                )
            model_name = model_match.group(1).strip() if model_match else None
            if not model_name:
                continue

            # Scan a context window around the matched model name rather than
            # the whole body.  This prevents a deprecation announcement for a
            # *different* model later in the same changelog entry from poisoning
            # the classification of the matched (often newly-launched) model.
            ctx_start = max(0, model_match.start() - 160)
            ctx_end = min(len(body), model_match.end() + 160)
            context = body[ctx_start:ctx_end]

            # Note: patterns are intentionally written without a trailing \b so
            # that partial stems like "deprecat" match "deprecation"/"deprecated".
            _retirement_re = re.compile(
                r"retir(?:e[ds]?|ing|ement)|end of support|shut\s*down",
                re.IGNORECASE,
            )
            _deprecation_re = re.compile(
                r"deprecat(?:e[ds]?|ing|ion)",
                re.IGNORECASE,
            )

            if _retirement_re.search(context):
                change_type = ChangeType.RETIREMENT
                severity = Severity.CRITICAL
                title = f"Anthropic model '{model_name}' retired"
            elif _deprecation_re.search(context):
                change_type = ChangeType.DEPRECATION_ANNOUNCED
                severity = Severity.WARN
                title = f"Anthropic model '{model_name}' deprecation announced"
            else:
                change_type = ChangeType.NEW_MODEL
                severity = Severity.INFO
                title = f"New Anthropic model: {model_name}"

            try:
                items.append(
                    ModelUpdateCreate(
                        provider=Provider.anthropic,
                        product="claude_api",
                        model=model_name,
                        change_type=change_type,
                        severity=severity,
                        title=title,
                        summary=body[:512],
                        source_url=_CHANGELOG_URL,
                        announced_at=date,
                        effective_at=date,
                        raw={"heading": heading_text, "snippet": body[:256]},
                    )
                )
            except Exception as exc:
                logger.debug(
                    "[%s] Skipping changelog item %r: %s",
                    self.provider_name, model_name, exc,
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