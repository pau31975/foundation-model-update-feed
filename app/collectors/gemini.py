"""Collector for Google Gemini API model updates.

Parses:
- https://ai.google.dev/gemini-api/docs/deprecations  (deprecation table)
- https://ai.google.dev/gemini-api/docs/models         (model list)
- https://ai.google.dev/gemini-api/docs/changelog      (changelog, best-effort)

Falls back to a set of known-good seed entries if live parsing yields nothing,
so the feed always has representative data.
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

_DEPRECATIONS_URL = settings.gemini_source_urls[0]
_MODELS_URL = settings.gemini_source_urls[1]
_CHANGELOG_URL = settings.gemini_source_urls[2]
_VERTEX_MODEL_VERSIONS_URL = settings.gemini_source_urls[3]
_VERTEX_RELEASE_NOTES_URL = settings.gemini_source_urls[4]
_RSS_URL = settings.google_rss_url

# Matches Gemini model identifiers within free text.
_RSS_MODEL_RE = re.compile(
    r"\b(gemini[-\s]?(?:ultra|pro|flash|nano|exp|advanced|embed)?(?:[-\s]\d+(?:\.\d+)?(?:[-\s]\w+)?)?)\b",
    re.IGNORECASE,
)
# Title must contain an explicit release verb for an RSS entry to be tagged NEW_MODEL.
_RSS_RELEASE_RE = re.compile(
    r"\b(introduc\w*|launch\w*|releas\w*|now\s+available|generally\s+available"
    r"|new\s+model|debut\w*|unveil\w*)\b",
    re.IGNORECASE,
)
# A model name followed by a version number in the title strongly signals a new model post.
_RSS_MODEL_VERSION_RE = re.compile(
    r"^(gemini|nano\s+banana|lyria|veo|imagen)\s+\d+(\.\d+)?\b",
    re.IGNORECASE,
)


def _parse_date(text: str) -> datetime | None:
    """Try several common date formats and return a UTC datetime or None."""
    text = text.strip()
    formats = [
        "%B %d, %Y",   # January 15, 2025
        "%b %d, %Y",   # Jan 15, 2025
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Try extracting via regex: "YYYY-MM-DD" anywhere in text
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        try:
            return datetime.strptime(m.group(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


class GeminiCollector(BaseCollector):
    """Collects model lifecycle events from Google Gemini API documentation."""

    provider_name = "google"

    def collect(self) -> list[ModelUpdateCreate]:
        items: list[ModelUpdateCreate] = []

        items.extend(self._collect_rss())
        items.extend(self._collect_deprecations())
        items.extend(self._collect_changelog())
        items.extend(self._collect_vertex_model_versions())
        items.extend(self._collect_vertex_release_notes())

        logger.info("[%s] collected %d item(s)", self.provider_name, len(items))
        return items

    # ------------------------------------------------------------------
    # RSS feed (live new-model auto-detection)
    # ------------------------------------------------------------------

    def _collect_rss(self) -> list[ModelUpdateCreate]:
        """Parse the Google Gemini blog RSS feed and extract model-related entries."""
        results: list[ModelUpdateCreate] = []
        for entry in self._fetch_rss(_RSS_URL):
            text = f"{entry['title']} {entry['description']}"
            m = _RSS_MODEL_RE.search(text)
            model_name = m.group(1).strip() if m else None

            # Skip entries with no Gemini model name and no AI/model keyword
            if not model_name and not re.search(
                r"\b(gemini|model|api|deprecat)\b",
                text, re.IGNORECASE,
            ):
                continue

            if re.search(
                r"\b(deprecat|retire|shutdown|sunset|end.of.?life)\b",
                text, re.IGNORECASE,
            ):
                change_type = ChangeType.DEPRECATION_ANNOUNCED
                severity = Severity.WARN
            elif _RSS_RELEASE_RE.search(entry["title"]) or _RSS_MODEL_VERSION_RE.search(entry["title"]):
                # Explicit release verb OR title starts with ModelName Version
                change_type = ChangeType.NEW_MODEL
                severity = Severity.INFO
            else:
                # Product features, blog posts, benchmarks — not a major model event
                change_type = ChangeType.CAPABILITY_CHANGED
                severity = Severity.INFO

            title = (entry["title"] or "Gemini announcement")[:256]
            source_url = entry["link"] if entry["link"].startswith("http") else _RSS_URL
            try:
                results.append(
                    ModelUpdateCreate(
                        provider=Provider.google,
                        product="gemini_api",
                        model=model_name,
                        change_type=change_type,
                        severity=severity,
                        title=title,
                        summary=(entry["description"] or title)[:512],
                        source_url=source_url,
                        announced_at=entry["pub_date"],
                        effective_at=None,
                        raw={"source": "rss", "feed": _RSS_URL},
                    )
                )
            except Exception as exc:
                logger.debug("[%s] Skipping RSS entry %r: %s", self.provider_name, title, exc)
        return results

    # ------------------------------------------------------------------
    # Deprecations page
    # ------------------------------------------------------------------

    def _collect_deprecations(self) -> list[ModelUpdateCreate]:
        """Parse the Gemini API deprecations page for table-based entries."""
        html = self._fetch(_DEPRECATIONS_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        # Look for tables that contain model info
        for table in soup.find_all("table"):
            headers = [
                th.get_text(strip=True).lower()
                for th in table.find_all("th")
            ]
            if not headers:
                # Try first row as header
                rows = table.find_all("tr")
                if rows:
                    headers = [
                        td.get_text(strip=True).lower()
                        for td in rows[0].find_all(["th", "td"])
                    ]

            # Identify column indices by common header names
            col_map = self._map_columns(
                headers,
                {
                    "model": ["model", "model name", "model id"],
                    "deprecation": ["deprecation", "deprecated", "deprecation date"],
                    "shutdown": ["shutdown", "discontinued", "end of life", "retirement"],
                    "replacement": ["replacement", "successor", "use instead"],
                },
            )

            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if not cells or len(cells) < 2:
                    continue

                model_name = self._get(cells, col_map.get("model"))
                if not model_name:
                    continue

                dep_date_str = self._get(cells, col_map.get("deprecation"))
                shut_date_str = self._get(cells, col_map.get("shutdown"))
                replacement = self._get(cells, col_map.get("replacement"))

                dep_date = _parse_date(dep_date_str) if dep_date_str else None
                shut_date = _parse_date(shut_date_str) if shut_date_str else None

                if shut_date:
                    change_type = ChangeType.RETIREMENT
                    severity = Severity.CRITICAL
                    title = f"Gemini model '{model_name}' retirement"
                    summary = (
                        f"Model '{model_name}' is scheduled for retirement"
                        + (f" on {shut_date_str}" if shut_date_str else "")
                        + (f". Recommended replacement: {replacement}" if replacement else "")
                        + "."
                    )
                elif dep_date:
                    change_type = ChangeType.DEPRECATION_ANNOUNCED
                    severity = Severity.WARN
                    title = f"Gemini model '{model_name}' deprecated"
                    summary = (
                        f"Model '{model_name}' has been deprecated"
                        + (f" as of {dep_date_str}" if dep_date_str else "")
                        + (f". Recommended replacement: {replacement}" if replacement else "")
                        + "."
                    )
                else:
                    continue

                raw: dict[str, Any] = {
                    "model": model_name,
                    "deprecation_date": dep_date_str,
                    "shutdown_date": shut_date_str,
                    "replacement": replacement,
                }

                try:
                    items.append(
                        ModelUpdateCreate(
                            provider=Provider.google,
                            product="gemini_api",
                            model=model_name,
                            change_type=change_type,
                            severity=severity,
                            title=title,
                            summary=summary,
                            source_url=_DEPRECATIONS_URL,
                            announced_at=dep_date,
                            effective_at=shut_date,
                            raw=raw,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] Skipping row %r: %s", self.provider_name, cells, exc
                    )

        return items

    # ------------------------------------------------------------------
    # Changelog page (best-effort)
    # ------------------------------------------------------------------

    def _collect_changelog(self) -> list[ModelUpdateCreate]:
        """Best-effort parse of Gemini changelog for model lifecycle events.

        Each dated heading (h2 MMMM D, YYYY) is followed by a <ul> of bullet
        points.  Each bullet is processed individually so that deprecation /
        retirement announcements and new-model launches are all captured with
        the correct date and change-type.
        """
        html = self._fetch(_CHANGELOG_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        headings = soup.find_all(re.compile(r"^h[23]$"))
        for heading in headings:
            heading_text = heading.get_text(strip=True)
            date = _parse_date(heading_text)

            sibling = heading.next_sibling
            while sibling:
                # Skip whitespace NavigableString nodes between tags.
                if not isinstance(sibling, Tag):
                    sibling = sibling.next_sibling
                    continue
                if sibling.name in ("h2", "h3", "h4"):
                    break
                # Expand <ul>/<ol> into individual <li> items for per-entry handling;
                # other block elements (p, div, …) are treated as a single entry.
                candidates: list[Tag] = (
                    sibling.find_all("li", recursive=False)
                    if sibling.name in ("ul", "ol")
                    else [sibling]
                )
                for candidate in candidates:
                    self._process_changelog_entry(
                        candidate, date, heading_text, items
                    )
                sibling = sibling.next_sibling

        return items

    def _process_changelog_entry(
        self,
        tag: Tag,
        date: "datetime | None",
        heading_text: str,
        items: list[ModelUpdateCreate],
    ) -> None:
        """Classify and append a single changelog bullet/block."""
        text = tag.get_text(separator=" ", strip=True)
        if not text or len(text) < 20:
            return

        is_retirement = bool(re.search(
            r"\b(shut.?down|retire|end.of.?life)\b", text, re.IGNORECASE
        ))
        is_deprecation = bool(re.search(
            r"\b(deprecat)\b", text, re.IGNORECASE
        ))
        is_release = bool(
            _RSS_RELEASE_RE.search(text)
            or re.search(r"\b(launch\w*|released?)\b", text, re.IGNORECASE)
        )

        if not (is_retirement or is_deprecation or is_release):
            return
        if not re.search(r"\b(gemini|model|api)\b", text, re.IGNORECASE):
            return

        # Prefer <code> element contents as the most accurate model identifier.
        code_values = [c.get_text(strip=True) for c in tag.find_all("code")]
        model_name: str | None = next(
            (c for c in code_values if re.match(r"gemini[-\s]", c, re.IGNORECASE)),
            None,
        )
        if model_name is None:
            m = re.search(
                r"(gemini[-\s\d.]+(?:pro|flash|ultra|nano|exp|embed)?"
                r"(?:[-\s](?:preview|latest|[\d.]+\w*))?)",
                text, re.IGNORECASE,
            )
            model_name = m.group(1).strip() if m else None

        if is_retirement:
            change_type = ChangeType.RETIREMENT
            severity = Severity.CRITICAL
            title = (
                f"Gemini model '{model_name}' shut down"
                if model_name
                else f"Gemini retirement: {text[:80]}"
            )
        elif is_deprecation:
            change_type = ChangeType.DEPRECATION_ANNOUNCED
            severity = Severity.WARN
            title = (
                f"Gemini model '{model_name}' deprecation announced"
                if model_name
                else f"Gemini deprecation: {text[:80]}"
            )
        else:
            change_type = ChangeType.NEW_MODEL
            severity = Severity.INFO
            title = (
                f"New Gemini model: {model_name}"
                if model_name
                else f"Gemini: {text[:80]}"
            )

        try:
            items.append(
                ModelUpdateCreate(
                    provider=Provider.google,
                    product="gemini_api",
                    model=model_name,
                    change_type=change_type,
                    severity=severity,
                    title=title[:256],
                    summary=text[:512],
                    source_url=_CHANGELOG_URL,
                    announced_at=date,
                    effective_at=(
                        date
                        if change_type in (ChangeType.NEW_MODEL, ChangeType.RETIREMENT)
                        else None
                    ),
                    raw={"heading": heading_text, "snippet": text[:256]},
                )
            )
        except Exception as exc:
            logger.debug(
                "[%s] Skipping changelog entry: %s", self.provider_name, exc
            )

    # ------------------------------------------------------------------
    # Vertex AI — model versions / lifecycle
    # ------------------------------------------------------------------

    def _collect_vertex_model_versions(self) -> list[ModelUpdateCreate]:
        """Parse the Vertex AI model versions page for lifecycle events.

        The page lists Gemini model IDs available on Vertex AI alongside
        their stable version period and deprecation / unavailability dates.
        Only rows with at least a deprecation or retirement date are emitted.
        """
        html = self._fetch(_VERTEX_MODEL_VERSIONS_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                rows = table.find_all("tr")
                if rows:
                    headers = [
                        td.get_text(strip=True).lower()
                        for td in rows[0].find_all(["th", "td"])
                    ]

            col_map = self._map_columns(
                headers,
                {
                    "model": ["model", "model id", "model version", "model name"],
                    "deprecated": ["deprecated", "deprecation date", "deprecation"],
                    "unavailable": [
                        "unavailable", "end of life", "retirement", "discontinued",
                        "shutdown", "eol",
                    ],
                    "replacement": ["replacement", "successor", "use instead", "recommended"],
                },
            )

            if "model" not in col_map:
                continue

            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if not cells or len(cells) < 2:
                    continue

                model_name = self._get(cells, col_map.get("model"))
                if not model_name:
                    continue

                deprecated_str = self._get(cells, col_map.get("deprecated"))
                unavailable_str = self._get(cells, col_map.get("unavailable"))
                replacement = self._get(cells, col_map.get("replacement"))

                deprecated_date = _parse_date(deprecated_str) if deprecated_str else None
                unavailable_date = _parse_date(unavailable_str) if unavailable_str else None

                if unavailable_date:
                    change_type = ChangeType.RETIREMENT
                    severity = Severity.CRITICAL
                    title = f"Vertex AI model '{model_name}' retirement"
                    summary = (
                        f"Vertex AI model '{model_name}' is scheduled for retirement"
                        + (f" on {unavailable_str}" if unavailable_str else "")
                        + (f". Recommended replacement: {replacement}" if replacement else "")
                        + "."
                    )
                    announced_at = deprecated_date
                    effective_at = unavailable_date
                elif deprecated_date:
                    change_type = ChangeType.DEPRECATION_ANNOUNCED
                    severity = Severity.WARN
                    title = f"Vertex AI model '{model_name}' deprecated"
                    summary = (
                        f"Vertex AI model '{model_name}' has been deprecated"
                        + (f" as of {deprecated_str}" if deprecated_str else "")
                        + (f". Recommended replacement: {replacement}" if replacement else "")
                        + "."
                    )
                    announced_at = deprecated_date
                    effective_at = None
                else:
                    # No lifecycle dates — skip informational-only rows
                    continue

                raw: dict[str, Any] = {
                    "model": model_name,
                    "deprecated": deprecated_str,
                    "unavailable": unavailable_str,
                    "replacement": replacement,
                    "source": "vertex_model_versions",
                }

                try:
                    items.append(
                        ModelUpdateCreate(
                            provider=Provider.google,
                            product="vertex_ai",
                            model=model_name,
                            change_type=change_type,
                            severity=severity,
                            title=title,
                            summary=summary,
                            source_url=_VERTEX_MODEL_VERSIONS_URL,
                            announced_at=announced_at,
                            effective_at=effective_at,
                            raw=raw,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] Skipping Vertex AI model versions row %r: %s",
                        self.provider_name, cells, exc,
                    )

        return items

    # ------------------------------------------------------------------
    # Vertex AI — generative AI release notes
    # ------------------------------------------------------------------

    def _collect_vertex_release_notes(self) -> list[ModelUpdateCreate]:
        """Best-effort parse of the Vertex AI generative AI release notes page.

        The page is structured as monthly / daily h2-h3 headings followed by
        bullet-point descriptions.  Only sections that mention a Gemini model
        name are emitted.
        """
        html = self._fetch(_VERTEX_RELEASE_NOTES_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        headings = soup.find_all(re.compile(r"^h[23]$"))
        for heading in headings:
            heading_text = heading.get_text(strip=True)
            date = _parse_date(heading_text)

            sibling = heading.next_sibling
            while sibling and isinstance(sibling, Tag) and sibling.name not in ("h2", "h3"):
                sibling_text = sibling.get_text(separator=" ", strip=True)

                if not re.search(
                    r"\b(gemini|vertex[\s-]?ai|model|launch|available|release|deprecat|retire)\b",
                    sibling_text, re.IGNORECASE,
                ):
                    sibling = sibling.next_sibling if sibling else None
                    continue

                model_match = re.search(
                    r"(gemini[-\s\d\.]+(?:pro|flash|ultra|nano|exp)?(?:[-\s]\d+(?:\.\d+)?(?:[-\s]\w+)?)?)",
                    sibling_text, re.IGNORECASE,
                )
                model_name = model_match.group(1).strip() if model_match else None

                if re.search(
                    r"\b(deprecat|retire|shutdown|sunset|end.of.?life)\b",
                    sibling_text, re.IGNORECASE,
                ):
                    change_type = ChangeType.DEPRECATION_ANNOUNCED
                    severity = Severity.WARN
                elif _RSS_RELEASE_RE.search(sibling_text) or (
                    model_name and _RSS_MODEL_VERSION_RE.search(model_name)
                ):
                    change_type = ChangeType.NEW_MODEL
                    severity = Severity.INFO
                else:
                    change_type = ChangeType.CAPABILITY_CHANGED
                    severity = Severity.INFO

                if model_name and len(sibling_text) > 20:
                    try:
                        items.append(
                            ModelUpdateCreate(
                                provider=Provider.google,
                                product="vertex_ai",
                                model=model_name,
                                change_type=change_type,
                                severity=severity,
                                title=(
                                    f"Vertex AI release: {model_name}"
                                    if change_type == ChangeType.NEW_MODEL
                                    else f"Vertex AI update: {sibling_text[:80]}"
                                )[:256],
                                summary=sibling_text[:512],
                                source_url=_VERTEX_RELEASE_NOTES_URL,
                                announced_at=date,
                                effective_at=date if change_type == ChangeType.NEW_MODEL else None,
                                raw={
                                    "heading": heading_text,
                                    "snippet": sibling_text[:256],
                                    "source": "vertex_release_notes",
                                },
                            )
                        )
                    except Exception as exc:
                        logger.debug(
                            "[%s] Skipping Vertex AI release notes item: %s",
                            self.provider_name, exc,
                        )

                sibling = sibling.next_sibling if sibling else None

        return items

    # ------------------------------------------------------------------
    # Util
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