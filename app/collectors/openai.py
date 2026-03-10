"""Collector for OpenAI model updates.

Parses:
- https://platform.openai.com/docs/deprecations  (primary source)

The page is server-rendered HTML containing definition-list or table sections
per deprecated model.  We also surface known entries as a seed fallback.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from app.collectors.base import BaseCollector
from app.config import settings
from app.schemas import ChangeType, ModelUpdateCreate, Provider, Severity

logger = logging.getLogger(__name__)

_DEPRECATIONS_URL = settings.openai_source_urls[0]


def _parse_date(text: str) -> datetime | None:
    """Try common date formats and return UTC datetime or None."""
    text = re.sub(r"\s+", " ", text.strip())
    formats = [
        "%B %d, %Y",
        "%b %d, %Y",
        "%Y-%m-%d",
        "%B %Y",
        "%b %Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        try:
            return datetime.strptime(m.group(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _classify_severity(change_type: ChangeType) -> Severity:
    if change_type == ChangeType.RETIREMENT:
        return Severity.CRITICAL
    if change_type == ChangeType.DEPRECATION_ANNOUNCED:
        return Severity.WARN
    return Severity.INFO


class OpenAICollector(BaseCollector):
    """Collects model lifecycle events from the OpenAI platform documentation."""

    provider_name = "openai"

    def collect(self) -> list[ModelUpdateCreate]:
        items = self._collect_deprecations()

        if not items:
            logger.info(
                "[%s] Live parsing yielded no items – using seed data.",
                self.provider_name,
            )
            items = _SEED_ENTRIES

        logger.info("[%s] collected %d item(s)", self.provider_name, len(items))
        return items

    # ------------------------------------------------------------------
    # Deprecations page
    # ------------------------------------------------------------------

    def _collect_deprecations(self) -> list[ModelUpdateCreate]:
        html = self._fetch(_DEPRECATIONS_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        # Strategy 1: look for definition lists <dt>/<dd> pairs
        items.extend(self._parse_definition_lists(soup))

        # Strategy 2: fall back to scanning headings + paragraphs
        if not items:
            items.extend(self._parse_headings(soup))

        return items

    def _parse_definition_lists(self, soup: BeautifulSoup) -> list[ModelUpdateCreate]:
        """Parse <dl>/<dt>/<dd> structures common in OpenAI docs."""
        items: list[ModelUpdateCreate] = []
        for dl in soup.find_all("dl"):
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")
            for dt, dd in zip(dts, dds):
                model_name = dt.get_text(strip=True)
                description = dd.get_text(separator=" ", strip=True)
                if not model_name:
                    continue

                shut_match = re.search(
                    r"shutdown\s+(?:on|date[:\s]+)?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
                    description, re.IGNORECASE
                )
                dep_match = re.search(
                    r"deprecat\w*\s+(?:on|as of)?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
                    description, re.IGNORECASE
                )

                shut_date = _parse_date(shut_match.group(1)) if shut_match else None
                dep_date = _parse_date(dep_match.group(1)) if dep_match else None

                if shut_date:
                    change_type = ChangeType.RETIREMENT
                elif dep_date:
                    change_type = ChangeType.DEPRECATION_ANNOUNCED
                else:
                    change_type = ChangeType.DEPRECATION_ANNOUNCED

                severity = _classify_severity(change_type)

                try:
                    items.append(
                        ModelUpdateCreate(
                            provider=Provider.openai,
                            product="openai_api",
                            model=model_name,
                            change_type=change_type,
                            severity=severity,
                            title=f"OpenAI model '{model_name}' deprecated/retired",
                            summary=description[:512],
                            source_url=_DEPRECATIONS_URL,
                            announced_at=dep_date,
                            effective_at=shut_date or dep_date,
                            raw={
                                "model": model_name,
                                "description": description[:256],
                            },
                        )
                    )
                except Exception as exc:
                    logger.debug("[%s] Skipping DL item %r: %s", self.provider_name, model_name, exc)

        return items

    def _parse_headings(self, soup: BeautifulSoup) -> list[ModelUpdateCreate]:
        """Scan h2/h3 + following paragraphs for deprecation info."""
        items: list[ModelUpdateCreate] = []
        headings = soup.find_all(re.compile(r"^h[23]$"))
        for heading in headings:
            heading_text = heading.get_text(strip=True)
            # Collect the text of following siblings until the next heading
            body_parts: list[str] = []
            sibling = heading.next_sibling
            while sibling:
                if isinstance(sibling, Tag) and sibling.name in ("h2", "h3"):
                    break
                if isinstance(sibling, Tag):
                    body_parts.append(sibling.get_text(separator=" ", strip=True))
                sibling = sibling.next_sibling

            body = " ".join(body_parts).strip()
            if not body:
                continue

            # Only process sections that look deprecation-related
            if not re.search(r"\b(deprecat|shutdown|retire|legacy|end.of.?life)\b",
                              heading_text + " " + body, re.IGNORECASE):
                continue

            # Try to find model name (code-formatted or quoted)
            model_match = re.search(
                r"`([^`]+)`|\"([^\"]+)\"|'([^']+)'|"
                r"\b(gpt-[\w\d.-]+|text-[\w-]+|davinci|curie|babbage|ada|whisper[-\w]*|"
                r"dall-e[-\w]*|embedding[\w-]*|tts[-\w]*)",
                heading_text + " " + body, re.IGNORECASE
            )
            model_name: str | None = None
            if model_match:
                model_name = next(
                    (g for g in model_match.groups() if g), None
                )

            shut_match = re.search(
                r"shutdown\s+(?:on\s+)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
                body, re.IGNORECASE
            )
            dep_match = re.search(
                r"deprecat\w*\s+(?:on\s+)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
                body, re.IGNORECASE
            )

            shut_date = _parse_date(shut_match.group(1)) if shut_match else None
            dep_date = _parse_date(dep_match.group(1)) if dep_match else None

            change_type = ChangeType.RETIREMENT if shut_date else ChangeType.DEPRECATION_ANNOUNCED
            severity = _classify_severity(change_type)
            title = heading_text[:256] if heading_text else f"OpenAI deprecation: {model_name}"

            try:
                items.append(
                    ModelUpdateCreate(
                        provider=Provider.openai,
                        product="openai_api",
                        model=model_name,
                        change_type=change_type,
                        severity=severity,
                        title=title,
                        summary=body[:512],
                        source_url=_DEPRECATIONS_URL,
                        announced_at=dep_date,
                        effective_at=shut_date or dep_date,
                        raw={"heading": heading_text, "snippet": body[:256]},
                    )
                )
            except Exception as exc:
                logger.debug(
                    "[%s] Skipping heading section %r: %s",
                    self.provider_name, heading_text, exc
                )

        return items


# ---------------------------------------------------------------------------
# Seed / fallback data – well-known OpenAI deprecations
# ---------------------------------------------------------------------------

_SEED_ENTRIES: list[ModelUpdateCreate] = [
    ModelUpdateCreate(
        provider=Provider.openai,
        product="openai_api",
        model="gpt-4-0314",
        change_type=ChangeType.RETIREMENT,
        severity=Severity.CRITICAL,
        title="GPT-4 0314 shutdown",
        summary=(
            "gpt-4-0314 was shut down on June 6, 2024. "
            "Migrate to gpt-4-turbo or gpt-4o."
        ),
        source_url=_DEPRECATIONS_URL,
        announced_at=datetime(2024, 4, 5, tzinfo=timezone.utc),
        effective_at=datetime(2024, 6, 6, tzinfo=timezone.utc),
        raw={"source": "seed", "replacement": "gpt-4-turbo"},
    ),
    ModelUpdateCreate(
        provider=Provider.openai,
        product="openai_api",
        model="gpt-3.5-turbo-0301",
        change_type=ChangeType.RETIREMENT,
        severity=Severity.CRITICAL,
        title="GPT-3.5 Turbo 0301 shutdown",
        summary=(
            "gpt-3.5-turbo-0301 was shut down on September 13, 2023. "
            "Use gpt-3.5-turbo instead."
        ),
        source_url=_DEPRECATIONS_URL,
        announced_at=datetime(2023, 6, 1, tzinfo=timezone.utc),
        effective_at=datetime(2023, 9, 13, tzinfo=timezone.utc),
        raw={"source": "seed", "replacement": "gpt-3.5-turbo"},
    ),
    ModelUpdateCreate(
        provider=Provider.openai,
        product="openai_api",
        model="text-davinci-003",
        change_type=ChangeType.RETIREMENT,
        severity=Severity.CRITICAL,
        title="Legacy Completions models (text-davinci-003 etc.) shutdown",
        summary=(
            "The text-davinci-003, text-davinci-002 and other legacy Completions "
            "models were shut down on January 4, 2024. Migrate to gpt-3.5-turbo or "
            "gpt-4o-mini via the Chat Completions API."
        ),
        source_url=_DEPRECATIONS_URL,
        announced_at=datetime(2023, 7, 6, tzinfo=timezone.utc),
        effective_at=datetime(2024, 1, 4, tzinfo=timezone.utc),
        raw={"source": "seed", "replacement": "gpt-3.5-turbo"},
    ),
    ModelUpdateCreate(
        provider=Provider.openai,
        product="openai_api",
        model="gpt-4o",
        change_type=ChangeType.NEW_MODEL,
        severity=Severity.INFO,
        title="GPT-4o launched",
        summary=(
            "gpt-4o ('omni') is OpenAI's newest flagship model. It matches GPT-4 Turbo "
            "performance on text at significantly lower cost and with faster response times. "
            "Natively multimodal across text, vision, and audio."
        ),
        source_url="https://platform.openai.com/docs/models",
        announced_at=datetime(2024, 5, 13, tzinfo=timezone.utc),
        effective_at=datetime(2024, 5, 13, tzinfo=timezone.utc),
        raw={"source": "seed"},
    ),
]
