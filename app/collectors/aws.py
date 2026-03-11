"""AWS Bedrock model update collector.

Parses:
- https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html
  (model lifecycle table: model ID, status, deprecation date, end-of-support date)
- https://docs.aws.amazon.com/bedrock/latest/userguide/doc-history.html
  (document history changelog for new model announcements)
- https://docs.aws.amazon.com/bedrock/latest/userguide/release-notes.html
  (full release notes for model availability and retirement events)

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

_LIFECYCLE_URL = settings.aws_source_urls[0]
_DOC_HISTORY_URL = settings.aws_source_urls[1]
_RELEASE_NOTES_URL = settings.aws_source_urls[2]
_RSS_URL = settings.aws_rss_url

# Filters RSS entries to only Bedrock / AI foundation model-related entries.
_AWS_BEDROCK_RE = re.compile(
    r"\b(bedrock|foundation\s+model|amazon\s+nova|amazon\s+titan|"
    r"llama\s*\d|mistral\s*\d|cohere\s+command|stability\s+\w+|"
    r"nova[-\s](?:pro|lite|micro|premier|sonic|canvas|reel))\b",
    re.IGNORECASE,
)

# Extracts a model identifier from AWS RSS text.
_RSS_MODEL_RE = re.compile(
    r"\b(amazon\s+nova[-\s]\w+|amazon\s+titan[-\s]\w+|"
    r"claude[-\s][\d.]+(?:[-\s]\w+)?|claude\s+\d+(?:\.\d+)?(?:\s+\w+)?|"
    r"llama[-\s\d.]+[\w.-]*|mistral[-\s\w]+|nova[-\s](?:pro|lite|micro|premier)|"
    r"titan[-\s]\w+)\b",
    re.IGNORECASE,
)
# Title must contain an explicit release verb for an RSS entry to be tagged NEW_MODEL.
_RSS_RELEASE_RE = re.compile(
    r"\b(introduc\w*|launch\w*|releas\w*|now\s+available|generally\s+available"
    r"|new\s+model|debut\w*|unveil\w*)\b",
    re.IGNORECASE,
)


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


class AWSCollector(BaseCollector):
    """Collects model lifecycle events from AWS Bedrock documentation."""

    provider_name = "aws"

    def collect(self) -> list[ModelUpdateCreate]:
        items: list[ModelUpdateCreate] = []

        items.extend(self._collect_rss())
        items.extend(self._collect_lifecycle_page())
        items.extend(self._collect_doc_history())

        if not items:
            logger.info(
                "[%s] Live parsing yielded no items – using seed data.",
                self.provider_name,
            )
        # Always include seed entries; DB fingerprint deduplication handles duplicates.
        items.extend(_SEED_ENTRIES)
        logger.info("[%s] collected %d item(s)", self.provider_name, len(items))
        return items

    # ------------------------------------------------------------------
    # RSS feed (live new-model auto-detection)
    # ------------------------------------------------------------------

    def _collect_rss(self) -> list[ModelUpdateCreate]:
        """Parse AWS What's New RSS feed and extract Bedrock model entries."""
        results: list[ModelUpdateCreate] = []
        for entry in self._fetch_rss(_RSS_URL):
            text = f"{entry['title']} {entry['description']}"
            if not _AWS_BEDROCK_RE.search(text):
                continue

            m = _RSS_MODEL_RE.search(text)
            model_name = m.group(1).strip() if m else None

            # Skip entries that have no specific model match and no Bedrock in title
            # (avoids generic AWS service announcements that mention Bedrock tangentially)
            if not model_name and not re.search(r"\bbedrock\b", entry["title"], re.IGNORECASE):
                continue

            if re.search(
                r"\b(deprecat|retire|end.of.?support|end.of.?life|sunset)\b",
                text, re.IGNORECASE,
            ):
                change_type = ChangeType.DEPRECATION_ANNOUNCED
                severity = Severity.WARN
            elif model_name and _RSS_RELEASE_RE.search(entry["title"]):
                change_type = ChangeType.NEW_MODEL
                severity = Severity.INFO
            else:
                # Bedrock service/feature updates — not a new foundation model
                change_type = ChangeType.CAPABILITY_CHANGED
                severity = Severity.INFO

            title = (entry["title"] or "AWS Bedrock announcement")[:256]
            source_url = entry["link"] if entry["link"].startswith("http") else _RSS_URL
            try:
                results.append(
                    ModelUpdateCreate(
                        provider=Provider.aws,
                        product="aws_bedrock",
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
    # Model lifecycle page
    # ------------------------------------------------------------------

    def _collect_lifecycle_page(self) -> list[ModelUpdateCreate]:
        """Parse the AWS Bedrock model lifecycle table."""
        html = self._fetch(_LIFECYCLE_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        for table in soup.find_all("table"):
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

            # Only process tables that look like model lifecycle tables
            if not re.search(r"\b(model|model id|model name)\b", " ".join(headers)):
                continue

            col_map = self._map_columns(
                headers,
                {
                    "model": ["model id", "model name", "model"],
                    "status": ["status", "state"],
                    "deprecation": ["deprecation date", "deprecated", "deprecation"],
                    "end_of_support": [
                        "end-of-support",
                        "end of support",
                        "retirement date",
                        "discontinued",
                        "shutdown",
                    ],
                    "replacement": ["replacement", "successor", "use instead"],
                },
            )

            if col_map.get("model") is None:
                continue

            for row in rows:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if not cells or len(cells) < 2:
                    continue

                model_name = self._get(cells, col_map.get("model"))
                if not model_name:
                    continue

                status_str = self._get(cells, col_map.get("status")) or ""
                dep_date_str = self._get(cells, col_map.get("deprecation"))
                eos_date_str = self._get(cells, col_map.get("end_of_support"))
                replacement = self._get(cells, col_map.get("replacement"))

                dep_date = _parse_date(dep_date_str) if dep_date_str else None
                eos_date = _parse_date(eos_date_str) if eos_date_str else None

                row_text = " ".join(cells).lower()
                is_inactive = (
                    re.search(
                        r"\b(deprecat|retire|end of support|discontinued|legacy)\b",
                        row_text,
                    )
                    or dep_date is not None
                    or eos_date is not None
                )
                if not is_inactive:
                    continue

                if eos_date:
                    change_type = ChangeType.RETIREMENT
                    severity = Severity.CRITICAL
                    title = f"AWS Bedrock model '{model_name}' retiring"
                    summary = (
                        f"'{model_name}' on AWS Bedrock will reach end of support"
                        + (f" on {eos_date_str}" if eos_date_str else "")
                        + (
                            f" (deprecated {dep_date_str})"
                            if dep_date_str and dep_date_str != eos_date_str
                            else ""
                        )
                        + (f". Replacement: {replacement}" if replacement else "")
                        + "."
                    )
                    effective = eos_date
                elif dep_date:
                    change_type = ChangeType.DEPRECATION_ANNOUNCED
                    severity = Severity.WARN
                    title = f"AWS Bedrock model '{model_name}' deprecated"
                    summary = (
                        f"'{model_name}' on AWS Bedrock has been deprecated"
                        + (f" as of {dep_date_str}" if dep_date_str else "")
                        + (f". Replacement: {replacement}" if replacement else "")
                        + "."
                    )
                    effective = dep_date
                else:
                    change_type = ChangeType.DEPRECATION_ANNOUNCED
                    severity = Severity.WARN
                    title = f"AWS Bedrock model '{model_name}' deprecated"
                    summary = (
                        f"'{model_name}' on AWS Bedrock has been deprecated"
                        + (f". Replacement: {replacement}" if replacement else "")
                        + "."
                    )
                    effective = None

                try:
                    items.append(
                        ModelUpdateCreate(
                            provider=Provider.aws,
                            product="aws_bedrock",
                            model=model_name,
                            change_type=change_type,
                            severity=severity,
                            title=title,
                            summary=summary,
                            source_url=_LIFECYCLE_URL,
                            announced_at=dep_date,
                            effective_at=effective,
                            raw={
                                "model": model_name,
                                "status": status_str,
                                "deprecation_date": dep_date_str,
                                "end_of_support_date": eos_date_str,
                                "replacement": replacement,
                            },
                        )
                    )
                except Exception as exc:
                    logger.debug(
                        "[%s] Skipping lifecycle row %r: %s",
                        self.provider_name, model_name, exc,
                    )

        return items

    # ------------------------------------------------------------------
    # Document history / changelog
    # ------------------------------------------------------------------

    def _collect_doc_history(self) -> list[ModelUpdateCreate]:
        """Best-effort parse of the AWS Bedrock doc history for new model announcements."""
        html = self._fetch(_DOC_HISTORY_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        # Doc history is typically a three-column table: Change | Description | Date
        for table in soup.find_all("table"):
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
                    "change": ["change", "update", "feature", "change description"],
                    "description": ["description", "detail", "notes"],
                    "date": ["date", "released", "updated"],
                },
            )

            for row in rows:
                cells = [
                    td.get_text(separator=" ", strip=True)
                    for td in row.find_all(["td", "th"])
                ]
                if not cells or len(cells) < 2:
                    continue

                change_text = self._get(cells, col_map.get("change")) or ""
                desc_text = self._get(cells, col_map.get("description")) or ""
                date_str = self._get(cells, col_map.get("date"))
                combined = f"{change_text} {desc_text}".strip()

                # Only look for model-related entries
                if not re.search(
                    r"\b(model|bedrock|claude|llama|titan|nova|mistral|"
                    r"cohere|ai21|jurassic)\b",
                    combined, re.IGNORECASE,
                ):
                    continue

                if not re.search(
                    r"\b(new|added|launch|available|deprecat|retire|support)\b",
                    combined, re.IGNORECASE,
                ):
                    continue

                entry_date = _parse_date(date_str) if date_str else None

                model_match = re.search(
                    r"((?:anthropic\.|amazon\.|meta\.|cohere\.|mistral\.|ai21\.)"
                    r"[\w\-\.]+(?:v\d+)?(?::\d+)?|"
                    r"(?:claude|llama|titan|nova|mistral|jurassic)[\s\-][\w\-\.]+)",
                    combined, re.IGNORECASE,
                )
                model_name: str | None = (
                    model_match.group(1).strip() if model_match else None
                )

                if re.search(
                    r"\b(deprecat|retire|end of support)\b", combined, re.IGNORECASE
                ):
                    change_type = ChangeType.DEPRECATION_ANNOUNCED
                    severity = Severity.WARN
                else:
                    change_type = ChangeType.NEW_MODEL
                    severity = Severity.INFO

                title = (
                    f"AWS Bedrock: {model_name}"
                    if model_name
                    else f"AWS Bedrock update: {change_text[:80]}"
                )

                try:
                    items.append(
                        ModelUpdateCreate(
                            provider=Provider.aws,
                            product="aws_bedrock",
                            model=model_name,
                            change_type=change_type,
                            severity=severity,
                            title=title,
                            summary=combined[:512],
                            source_url=_DOC_HISTORY_URL,
                            announced_at=entry_date,
                            effective_at=entry_date,
                            raw={
                                "change": change_text[:256],
                                "description": desc_text[:256],
                                "date": date_str,
                            },
                        )
                    )
                except Exception as exc:
                    logger.debug(
                        "[%s] Skipping doc history row: %s", self.provider_name, exc
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
# Seed / fallback data – comprehensive AWS Bedrock model lifecycle events
# ---------------------------------------------------------------------------

_SEED_ENTRIES: list[ModelUpdateCreate] = [
    # ── New model releases ────────────────────────────────────────────────
    ModelUpdateCreate(
        provider=Provider.aws,
        product="aws_bedrock",
        model="amazon.nova-pro-v1:0",
        change_type=ChangeType.NEW_MODEL,
        severity=Severity.INFO,
        title="AWS Bedrock Amazon Nova Pro available",
        summary=(
            "amazon.nova-pro-v1:0 is available on AWS Bedrock. "
            "Amazon Nova Pro is a highly capable multimodal model for complex enterprise "
            "tasks with an optimal accuracy-to-speed tradeoff. Released December 3, 2024."
        ),
        source_url=_DOC_HISTORY_URL,
        announced_at=datetime(2024, 12, 3, tzinfo=timezone.utc),
        effective_at=datetime(2024, 12, 3, tzinfo=timezone.utc),
        raw={"source": "seed"},
    ),
    ModelUpdateCreate(
        provider=Provider.aws,
        product="aws_bedrock",
        model="amazon.nova-lite-v1:0",
        change_type=ChangeType.NEW_MODEL,
        severity=Severity.INFO,
        title="AWS Bedrock Amazon Nova Lite available",
        summary=(
            "amazon.nova-lite-v1:0 is available on AWS Bedrock. "
            "Nova Lite is a very low-cost multimodal model fast enough for real-time applications. "
            "Released December 3, 2024."
        ),
        source_url=_DOC_HISTORY_URL,
        announced_at=datetime(2024, 12, 3, tzinfo=timezone.utc),
        effective_at=datetime(2024, 12, 3, tzinfo=timezone.utc),
        raw={"source": "seed"},
    ),
    ModelUpdateCreate(
        provider=Provider.aws,
        product="aws_bedrock",
        model="amazon.nova-micro-v1:0",
        change_type=ChangeType.NEW_MODEL,
        severity=Severity.INFO,
        title="AWS Bedrock Amazon Nova Micro available",
        summary=(
            "amazon.nova-micro-v1:0 is available on AWS Bedrock. "
            "Nova Micro is a text-only model delivering the lowest latency and cost in the Nova family. "
            "Released December 3, 2024."
        ),
        source_url=_DOC_HISTORY_URL,
        announced_at=datetime(2024, 12, 3, tzinfo=timezone.utc),
        effective_at=datetime(2024, 12, 3, tzinfo=timezone.utc),
        raw={"source": "seed"},
    ),
    ModelUpdateCreate(
        provider=Provider.aws,
        product="aws_bedrock",
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        change_type=ChangeType.NEW_MODEL,
        severity=Severity.INFO,
        title="AWS Bedrock Claude 3.5 Sonnet v2 available",
        summary=(
            "anthropic.claude-3-5-sonnet-20241022-v2:0 is available on AWS Bedrock. "
            "Upgraded Claude 3.5 Sonnet with improved coding, reasoning, and computer use "
            "(beta). Released October 2024."
        ),
        source_url=_DOC_HISTORY_URL,
        announced_at=datetime(2024, 10, 22, tzinfo=timezone.utc),
        effective_at=datetime(2024, 10, 22, tzinfo=timezone.utc),
        raw={"source": "seed"},
    ),
    # ── Retirements & deprecations ────────────────────────────────────────
    ModelUpdateCreate(
        provider=Provider.aws,
        product="aws_bedrock",
        model="anthropic.claude-v2:1",
        change_type=ChangeType.RETIREMENT,
        severity=Severity.CRITICAL,
        title="AWS Bedrock Claude 2.1 (anthropic.claude-v2:1) retired",
        summary=(
            "anthropic.claude-v2:1 on AWS Bedrock reached end of support on March 1, 2025. "
            "Migrate to anthropic.claude-3-5-sonnet-20241022-v2:0 or amazon.nova-pro-v1:0."
        ),
        source_url=_LIFECYCLE_URL,
        announced_at=datetime(2024, 11, 1, tzinfo=timezone.utc),
        effective_at=datetime(2025, 3, 1, tzinfo=timezone.utc),
        raw={"source": "seed", "replacement": "anthropic.claude-3-5-sonnet-20241022-v2:0"},
    ),
    ModelUpdateCreate(
        provider=Provider.aws,
        product="aws_bedrock",
        model="anthropic.claude-instant-v1",
        change_type=ChangeType.RETIREMENT,
        severity=Severity.CRITICAL,
        title="AWS Bedrock Claude Instant v1 retired",
        summary=(
            "anthropic.claude-instant-v1 on AWS Bedrock reached end of support "
            "on September 30, 2024. Migrate to anthropic.claude-3-haiku-20240307-v1:0."
        ),
        source_url=_LIFECYCLE_URL,
        announced_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        effective_at=datetime(2024, 9, 30, tzinfo=timezone.utc),
        raw={"source": "seed", "replacement": "anthropic.claude-3-haiku-20240307-v1:0"},
    ),
    ModelUpdateCreate(
        provider=Provider.aws,
        product="aws_bedrock",
        model="anthropic.claude-v2",
        change_type=ChangeType.RETIREMENT,
        severity=Severity.CRITICAL,
        title="AWS Bedrock Claude 2 (anthropic.claude-v2) retired",
        summary=(
            "anthropic.claude-v2 on AWS Bedrock reached end of support on August 1, 2024. "
            "Migrate to anthropic.claude-3-haiku-20240307-v1:0 or later."
        ),
        source_url=_LIFECYCLE_URL,
        announced_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
        effective_at=datetime(2024, 8, 1, tzinfo=timezone.utc),
        raw={"source": "seed", "replacement": "anthropic.claude-3-haiku-20240307-v1:0"},
    ),
]
