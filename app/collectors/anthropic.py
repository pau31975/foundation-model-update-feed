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
_CHANGELOG_URL = settings.anthropic_source_urls[1]
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
        # Always include seed entries; DB fingerprint deduplication handles duplicates.
        items.extend(_SEED_ENTRIES)
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
                    "model": ["model name", "api model name", "api name", "model"],
                    "status": ["status", "availability", "support"],
                    "deprecation": ["deprecat", "end of support", "sunset", "retirement"],
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

            model_match = re.search(
                r"(claude[-\s][\w\-\.]+(?:\d{8})?)",
                body, re.IGNORECASE,
            )
            model_name = model_match.group(1).strip() if model_match else None
            if not model_name:
                continue

            if re.search(r"\b(deprecat|retire|end of support|sunset)\b", body, re.IGNORECASE):
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


# ---------------------------------------------------------------------------
# Seed / fallback data – well-known Anthropic model lifecycle events
# ---------------------------------------------------------------------------

# _SEED_ENTRIES: list[ModelUpdateCreate] = [
#     # ── New model releases ────────────────────────────────────────────────
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-opus-4-6",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Claude Opus 4.6 released – most intelligent Claude model",
#         summary=(
#             "claude-opus-4-6 is Anthropic's most intelligent model as of February 2026. "
#             "API identifier: claude-opus-4-6-20260205. Released February 5, 2026 "
#             "($5 / $25 per MTok input/output). Supports 200K token context and extended thinking."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-sonnet-4-6",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Claude Sonnet 4.6 released",
#         summary=(
#             "claude-sonnet-4-6 released February 17, 2026. API identifier: "
#             "claude-sonnet-4-6-20260217. Balanced performance and cost ($3 / $15 per MTok)."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2026, 2, 17, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 2, 17, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-opus-4-5",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Claude Opus 4.5 released",
#         summary=(
#             "claude-opus-4-5 released November 24, 2025. API identifier: "
#             "claude-opus-4-5-20251124. Extended thinking with 80K output token limit."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2025, 11, 24, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 11, 24, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-haiku-4-5",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Claude Haiku 4.5 released",
#         summary=(
#             "claude-haiku-4-5 released October 15, 2025. API identifier: "
#             "claude-haiku-4-5-20251015. Fast, lightweight model in the Claude 4 family."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2025, 10, 15, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 10, 15, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-sonnet-4-5",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Claude Sonnet 4.5 released",
#         summary=(
#             "claude-sonnet-4-5 released September 29, 2025. API identifier: "
#             "claude-sonnet-4-5-20250929. Replaces claude-sonnet-4 as the primary balanced model."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2025, 9, 29, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 9, 29, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-opus-4-20250514",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Claude Opus 4 and Sonnet 4 released",
#         summary=(
#             "claude-opus-4-20250514 and claude-sonnet-4-20250514 were released May 22, 2025. "
#             "Claude 4 generation focused on advanced reasoning, coding, and extended context. "
#             "claude-opus-4 with extended thinking supports up to 32K thinking tokens."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2025, 5, 22, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 5, 22, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-3-7-sonnet-20250219",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Claude 3.7 Sonnet released",
#         summary=(
#             "claude-3-7-sonnet-20250219 is Anthropic's first hybrid reasoning model, "
#             "featuring extended thinking for complex tasks. Released February 24, 2025. "
#             "Available via API and Claude.ai."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2025, 2, 24, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 2, 24, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-3-5-sonnet-20241022",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="Claude 3.5 Sonnet v2 released (Oct 2024)",
#         summary=(
#             "claude-3-5-sonnet-20241022 delivered improved coding, reasoning, and "
#             "instruction following, plus the computer use beta feature. Released October 22, 2024."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2024, 10, 22, tzinfo=timezone.utc),
#         effective_at=datetime(2024, 10, 22, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     # ── Depecations & retirements ────────────────────────────────────────
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-3-7-sonnet-20250219",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="Claude 3.7 Sonnet and Claude 3.5 Haiku retired",
#         summary=(
#             "claude-3-7-sonnet-20250219 and claude-3-5-haiku-20241022 were "
#             "retired on February 19, 2026. Migrate to claude-sonnet-4-6 for "
#             "comparable or improved capability."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2026, 1, 19, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 2, 19, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "claude-sonnet-4-6"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-3-haiku-20240307",
#         change_type=ChangeType.DEPRECATION_ANNOUNCED,
#         severity=Severity.WARN,
#         title="Claude 3 Haiku deprecated (retirement April 2026)",
#         summary=(
#             "claude-3-haiku-20240307 was deprecated February 19, 2026, "
#             "with retirement scheduled for April 19, 2026. "
#             "Migrate to claude-haiku-4-5."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2026, 2, 19, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "claude-haiku-4-5"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-3-5-sonnet-20240620",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="Claude 3.5 Sonnet (both versions) retired",
#         summary=(
#             "claude-3-5-sonnet-20240620 and claude-3-5-sonnet-20241022 were deprecated "
#             "August 13, 2025 and retired October 28, 2025. "
#             "Migrate to claude-sonnet-4-5 or later."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 8, 13, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 10, 28, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "claude-sonnet-4-5"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-3-opus-20240229",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="Claude 3 Opus retired",
#         summary=(
#             "claude-3-opus-20240229 was deprecated June 30, 2025 and retired January 5, 2026. "
#             "Migrate to claude-opus-4-5 or claude-opus-4-6."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 6, 30, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "claude-opus-4-5"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-2.0",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="Claude 2.0, 2.1, and Claude Sonnet 3 retired",
#         summary=(
#             "claude-2.0, claude-2.1, and claude-3-sonnet-20240229 were deprecated "
#             "January 21, 2025 and retired July 21, 2025. "
#             "Migrate to claude-3-5-sonnet-20241022 or later."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 1, 21, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 7, 21, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "claude-3-5-sonnet-20241022"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.anthropic,
#         product="claude_api",
#         model="claude-instant-1.2",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="Claude Instant 1.2 retired",
#         summary=(
#             "claude-instant-1.2 was retired on March 14, 2025. "
#             "Migrate to claude-haiku-4-5 for a comparable low-latency option."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 1, 14, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 3, 14, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "claude-haiku-4-5"},
#     ),
# ]
