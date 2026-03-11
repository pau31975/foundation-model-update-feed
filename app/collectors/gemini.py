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

        # Always include seed entries; DB fingerprint deduplication handles duplicates.
        items.extend(_SEED_ENTRIES)
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
            elif model_name and _RSS_RELEASE_RE.search(entry["title"]):
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
        """Best-effort parse of Gemini changelog for NEW_MODEL entries."""
        html = self._fetch(_CHANGELOG_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        # Look for heading + paragraph patterns typical in changelog pages
        headings = soup.find_all(re.compile(r"^h[23]$"))
        for heading in headings:
            heading_text = heading.get_text(strip=True)
            date = _parse_date(heading_text)

            # Walk sibling paragraphs / list items under this heading
            sibling = heading.next_sibling
            while sibling and isinstance(sibling, Tag) and sibling.name not in ("h2", "h3", "h4"):
                sibling_text = sibling.get_text(separator=" ", strip=True)
                # Look for entries mentioning "new model" or specific model names
                if re.search(r"\b(gemini[-\s]\w+|model|launch|available|release)\b",
                             sibling_text, re.IGNORECASE):
                    # Try to extract model name
                    model_match = re.search(
                        r"(gemini[-\s\d\.]+(?:pro|flash|ultra|nano)?(?:[-\s]\d+\.\d+|\w+)?)",
                        sibling_text, re.IGNORECASE
                    )
                    model_name = model_match.group(1) if model_match else None

                    if model_name and len(sibling_text) > 20:
                        try:
                            items.append(
                                ModelUpdateCreate(
                                    provider=Provider.google,
                                    product="gemini_api",
                                    model=model_name.strip(),
                                    change_type=ChangeType.NEW_MODEL,
                                    severity=Severity.INFO,
                                    title=f"New Gemini model: {model_name.strip()}",
                                    summary=sibling_text[:512],
                                    source_url=_CHANGELOG_URL,
                                    announced_at=date,
                                    effective_at=date,
                                    raw={"heading": heading_text, "snippet": sibling_text[:256]},
                                )
                            )
                        except Exception as exc:
                            logger.debug("[%s] Skipping changelog item: %s", self.provider_name, exc)

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


# ---------------------------------------------------------------------------
# Seed / fallback data – comprehensive Google Gemini lifecycle events
# ---------------------------------------------------------------------------

# _SEED_ENTRIES: list[ModelUpdateCreate] = [
#     # ── New model releases ────────────────────────────────────────────────
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-3.1-pro-preview",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Gemini 3.1 Pro Preview released – newest Gemini model",
#         summary=(
#             "gemini-3.1-pro-preview released February 19, 2026. "
#             "Next-generation frontier model with improved reasoning, code generation, "
#             "and multimodal capabilities over Gemini 3 Pro."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2026, 2, 19, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 2, 19, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-3-flash-preview",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Gemini 3 Flash Preview released",
#         summary=(
#             "gemini-3-flash-preview released December 17, 2025. "
#             "Fast, multimodal model in the Gemini 3 family for high-throughput workloads."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2025, 12, 17, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 12, 17, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-2.5-pro",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Gemini 2.5 Pro and Flash released",
#         summary=(
#             "gemini-2.5-pro and gemini-2.5-flash released June 17, 2025. "
#             "Gemini 2.5 Pro is Google's most advanced reasoning model with 2M token context. "
#             "Built-in thinking mode for complex problem solving."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2025, 6, 17, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 6, 17, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-2.0-flash",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Gemini 2.0 Flash generally available",
#         summary=(
#             "gemini-2.0-flash generally available February 5, 2025. "
#             "Improved performance and lower latency over 1.5 Flash with native tool use."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2025, 2, 5, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 2, 5, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-3-pro-preview",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Gemini 3 Pro Preview released",
#         summary=(
#             "gemini-3-pro-preview released November 18, 2025. "
#             "Frontier preview model in the Gemini 3 family ahead of the stable release."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2025, 11, 18, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 11, 18, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     # ── Deprecations & retirements ────────────────────────────────────────
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-3-pro-preview",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="Gemini 3 Pro Preview shut down",
#         summary=(
#             "gemini-3-pro-preview was deprecated February 26, 2026 and shut down "
#             "March 9, 2026. Migrate to gemini-3.1-pro-preview."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2026, 2, 26, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 3, 9, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gemini-3.1-pro-preview"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-2.5-pro",
#         change_type=ChangeType.DEPRECATION_ANNOUNCED,
#         severity=Severity.WARN,
#         title="Gemini 2.5 Pro deprecated (shutdown June 2026)",
#         summary=(
#             "gemini-2.5-pro deprecated June 17, 2025 with shutdown scheduled for "
#             "June 17, 2026. Migrate to gemini-3.1-pro-preview."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 6, 17, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gemini-3.1-pro-preview"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-2.5-flash",
#         change_type=ChangeType.DEPRECATION_ANNOUNCED,
#         severity=Severity.WARN,
#         title="Gemini 2.5 Flash deprecated (shutdown June 2026)",
#         summary=(
#             "gemini-2.5-flash deprecated June 17, 2025 with shutdown scheduled for "
#             "June 17, 2026. Migrate to gemini-3-flash-preview."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 6, 17, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gemini-3-flash-preview"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-2.0-flash",
#         change_type=ChangeType.DEPRECATION_ANNOUNCED,
#         severity=Severity.WARN,
#         title="Gemini 2.0 Flash deprecated (shutdown June 2026)",
#         summary=(
#             "gemini-2.0-flash deprecated February 5, 2025 with shutdown scheduled for "
#             "June 1, 2026. Migrate to gemini-2.5-flash."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 2, 5, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gemini-2.5-flash"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-2.0-flash-lite",
#         change_type=ChangeType.DEPRECATION_ANNOUNCED,
#         severity=Severity.WARN,
#         title="Gemini 2.0 Flash-Lite deprecated (shutdown June 2026)",
#         summary=(
#             "gemini-2.0-flash-lite deprecated February 25, 2025 with shutdown scheduled for "
#             "June 1, 2026. Migrate to gemini-2.5-flash-lite."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 2, 25, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gemini-2.5-flash-lite"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="text-embedding-004",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="text-embedding-004 retired",
#         summary=(
#             "text-embedding-004 was deprecated April 9, 2024 and shut down January 14, 2026. "
#             "Migrate to gemini-embedding-001."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2024, 4, 9, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 1, 14, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gemini-embedding-001"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.google,
#         product="gemini_api",
#         model="gemini-1.0-pro",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="Gemini 1.0 Pro retired",
#         summary=(
#             "gemini-1.0-pro and gemini-1.0-pro-001 shut down February 15, 2025. "
#             "Migrate to gemini-2.0-flash or later."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2024, 9, 19, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 2, 15, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gemini-2.0-flash"},
#     ),
# ]
