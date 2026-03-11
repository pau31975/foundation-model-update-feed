"""Collector for OpenAI model updates.

Parses:
- https://developers.openai.com/api/docs/deprecations  (deprecation history)
- https://developers.openai.com/api/docs/models           (model catalog)
- https://developers.openai.com/api/docs/changelog        (release changelog)

Also surfaces well-known entries as seed fallback data.
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
_MODELS_URL = settings.openai_source_urls[1]
_CHANGELOG_URL = settings.openai_source_urls[2]
_RSS_URL = settings.openai_rss_url

# Matches specific OpenAI model identifiers within free text.
_RSS_MODEL_RE = re.compile(
    r"\b(gpt-5(?:\.\d+)?(?:-\w+)*|gpt-4(?:o)?(?:[\d.-]\w*)?|o[1-4](?:-\w+)?|"
    r"gpt-image-\d+(?:\.\d+)?(?:-\w+)*|dall-e-\d+|sora(?:-\d+(?:-\w+)?)?|"
    r"whisper-\d+(?:\.\d+)?|text-embedding[-\w]+|tts-\d+(?:-\w+)?)\b",
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
        items: list[ModelUpdateCreate] = []
        items.extend(self._collect_rss())
        items.extend(self._collect_deprecations())
        items.extend(self._collect_changelog())
        logger.info("[%s] collected %d item(s)", self.provider_name, len(items))
        return items

    # ------------------------------------------------------------------
    # RSS feed (live new-model auto-detection)
    # ------------------------------------------------------------------

    def _collect_rss(self) -> list[ModelUpdateCreate]:
        """Parse the OpenAI blog RSS feed and extract model-related entries."""
        results: list[ModelUpdateCreate] = []
        for entry in self._fetch_rss(_RSS_URL):
            text = f"{entry['title']} {entry['description']}"
            m = _RSS_MODEL_RE.search(text)
            if not m:
                continue
            model_name = m.group(1)

            if re.search(
                r"\b(deprecat|retire|shutdown|sunset|end.of.?life)\b",
                text, re.IGNORECASE,
            ):
                change_type = ChangeType.DEPRECATION_ANNOUNCED
                severity = Severity.WARN
            elif _RSS_RELEASE_RE.search(entry["title"]):
                change_type = ChangeType.NEW_MODEL
                severity = Severity.INFO
            else:
                # Blog posts, case studies, feature updates — not a major model event
                change_type = ChangeType.CAPABILITY_CHANGED
                severity = Severity.INFO

            title = (entry["title"] or f"OpenAI {model_name} announcement")[:256]
            source_url = entry["link"] if entry["link"].startswith("http") else _RSS_URL
            try:
                results.append(
                    ModelUpdateCreate(
                        provider=Provider.openai,
                        product="openai_api",
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
    # Changelog page (new model announcements)
    # ------------------------------------------------------------------

    def _collect_changelog(self) -> list[ModelUpdateCreate]:
        """Best-effort parse of the OpenAI API changelog for new-model entries."""
        html = self._fetch(_CHANGELOG_URL)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items: list[ModelUpdateCreate] = []

        headings = soup.find_all(re.compile(r"^h[23]$"))
        for heading in headings:
            heading_text = heading.get_text(strip=True)

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

            # Only surface new-model releases
            if not re.search(
                r"\b(released?|launched?|new model|available)\b",
                body, re.IGNORECASE,
            ):
                continue

            model_match = re.search(
                r"\b(gpt-5\.?\d*(?:-\w+)?|gpt-4\.\d+(?:-\w+)?|o\d+(?:-\w+)?|"
                r"gpt-4o(?:-\w+)?|gpt-image-\d+(?:\.\d+)?(?:-\w+)?|"
                r"sora-\d+(?:-\w+)?|gpt-realtime-\d+(?:\.\d+)?)",
                body, re.IGNORECASE,
            )
            model_name: str | None = model_match.group(1).strip() if model_match else None
            if not model_name:
                continue

            date_match = re.search(r"(\w+ \d{1,2},?\s*\d{4}|\d{4}-\d{2}-\d{2})", heading_text)
            entry_date = _parse_date(date_match.group(1)) if date_match else None

            title = f"OpenAI {model_name} released"
            try:
                items.append(
                    ModelUpdateCreate(
                        provider=Provider.openai,
                        product="openai_api",
                        model=model_name,
                        change_type=ChangeType.NEW_MODEL,
                        severity=Severity.INFO,
                        title=title,
                        summary=body[:512],
                        source_url=_CHANGELOG_URL,
                        announced_at=entry_date,
                        effective_at=entry_date,
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
