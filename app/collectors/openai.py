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
        # Always include seed entries; DB fingerprint deduplication handles duplicates.
        items.extend(_SEED_ENTRIES)
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


# ---------------------------------------------------------------------------
# Seed / fallback data – comprehensive OpenAI model lifecycle events
# ---------------------------------------------------------------------------

# _SEED_ENTRIES: list[ModelUpdateCreate] = [
#     # ── New model releases ────────────────────────────────────────────────
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="gpt-5.4",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="GPT-5.4 released – newest OpenAI frontier model",
#         summary=(
#             "gpt-5.4 is OpenAI's newest flagship model for professional work, complex "
#             "reasoning, and coding. Released to the Chat Completions and Responses API "
#             "on March 5, 2026. Supports a 1M token context window, native computer use, "
#             "tool search, and built-in compaction for long-running agent workflows."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": None},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="gpt-5.2",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="GPT-5.2 released",
#         summary=(
#             "gpt-5.2 is the newest model in the GPT-5 family, released December 11, 2025. "
#             "Improvements over GPT-5.1 in general intelligence, instruction following, "
#             "accuracy, multimodality (especially vision), and front-end code generation."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2025, 12, 11, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 12, 11, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="gpt-5.1",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="GPT-5.1 released",
#         summary=(
#             "gpt-5.1 released November 13, 2025. Especially proficient in steerability, "
#             "code generation, and agentic workflows. Also released gpt-5.1-codex and "
#             "gpt-5.1-codex-mini for agentic coding tasks."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2025, 11, 13, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 11, 13, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="gpt-5",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="GPT-5 family released (gpt-5, gpt-5-mini, gpt-5-nano)",
#         summary=(
#             "OpenAI released the GPT-5 model family on August 7, 2025, including gpt-5, "
#             "gpt-5-mini, and gpt-5-nano. These models support a new 'minimal' reasoning effort "
#             "value optimized for fast responses."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2025, 8, 7, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 8, 7, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="gpt-4.1",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="GPT-4.1 released (gpt-4.1, gpt-4.1-mini, gpt-4.1-nano)",
#         summary=(
#             "gpt-4.1, gpt-4.1-mini, and gpt-4.1-nano released April 14, 2025. "
#             "Feature improved instruction following, coding, and a 1M token context window. "
#             "gpt-4.1 and gpt-4.1-mini support supervised fine-tuning."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2025, 4, 14, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 4, 14, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="o3",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="o3 and o4-mini reasoning models released",
#         summary=(
#             "o3 and o4-mini released April 16, 2025. They set a new standard for math, "
#             "science, coding, visual reasoning, and technical writing."
#         ),
#         source_url=_CHANGELOG_URL,
#         announced_at=datetime(2025, 4, 16, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 4, 16, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="gpt-4o",
#         change_type=ChangeType.NEW_MODEL,
#         severity=Severity.INFO,
#         title="GPT-4o launched",
#         summary=(
#             "gpt-4o ('omni') is OpenAI's multimodal flagship model released May 13, 2024. "
#             "Matches GPT-4 Turbo performance at lower cost with faster response times. "
#             "Natively multimodal across text, vision, and audio."
#         ),
#         source_url=_MODELS_URL,
#         announced_at=datetime(2024, 5, 13, tzinfo=timezone.utc),
#         effective_at=datetime(2024, 5, 13, tzinfo=timezone.utc),
#         raw={"source": "seed"},
#     ),
#     # ── Deprecations & retirements ────────────────────────────────────────
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="chatgpt-4o-latest",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="chatgpt-4o-latest snapshot retired",
#         summary=(
#             "chatgpt-4o-latest was deprecated November 18, 2025 and shut down "
#             "February 17, 2026. Migrate to gpt-5.1-chat-latest."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 11, 18, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 2, 17, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gpt-5.1-chat-latest"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="codex-mini-latest",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="codex-mini-latest model retired",
#         summary=(
#             "codex-mini-latest was deprecated November 17, 2025 and removed from the API "
#             "on February 12, 2026. Migrate to gpt-5-codex-mini."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 11, 17, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 2, 12, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gpt-5-codex-mini"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="dall-e-2",
#         change_type=ChangeType.DEPRECATION_ANNOUNCED,
#         severity=Severity.WARN,
#         title="DALL-E 2 and DALL-E 3 deprecated",
#         summary=(
#             "dall-e-2 and dall-e-3 were deprecated November 14, 2025 with shutdown "
#             "scheduled for May 12, 2026. Migrate to gpt-image-1 or gpt-image-1-mini."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 11, 14, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gpt-image-1"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="gpt-4-0314",
#         change_type=ChangeType.DEPRECATION_ANNOUNCED,
#         severity=Severity.WARN,
#         title="Legacy GPT-4 snapshots deprecated (shutdown March 2026)",
#         summary=(
#             "gpt-4-0314, gpt-4-1106-preview, and gpt-4-0125-preview (including "
#             "gpt-4-turbo-preview) were deprecated September 26, 2025 with shutdown "
#             "on March 26, 2026. Migrate to gpt-5 or gpt-4.1."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 9, 26, tzinfo=timezone.utc),
#         effective_at=datetime(2026, 3, 26, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gpt-5 or gpt-4.1"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="o1-preview",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="o1-preview retired",
#         summary=(
#             "o1-preview was deprecated April 28, 2025 and shut down July 28, 2025. "
#             "Migrate to o3."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 4, 28, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 7, 28, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "o3"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="gpt-4.5-preview",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="gpt-4.5-preview retired",
#         summary=(
#             "gpt-4.5-preview was deprecated April 14, 2025 and shut down July 14, 2025. "
#             "Migrate to gpt-4.1."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2025, 4, 14, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 7, 14, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gpt-4.1"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="gpt-4-32k",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="GPT-4-32K retired",
#         summary=(
#             "gpt-4-32k and gpt-4-32k-0613 were deprecated June 6, 2024 and shut down "
#             "June 6, 2025. Migrate to gpt-4o."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2024, 6, 6, tzinfo=timezone.utc),
#         effective_at=datetime(2025, 6, 6, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gpt-4o"},
#     ),
#     ModelUpdateCreate(
#         provider=Provider.openai,
#         product="openai_api",
#         model="text-davinci-003",
#         change_type=ChangeType.RETIREMENT,
#         severity=Severity.CRITICAL,
#         title="Legacy Completions models (text-davinci-003 etc.) shutdown",
#         summary=(
#             "The text-davinci-003, text-davinci-002 and other legacy Completions "
#             "models were shut down on January 4, 2024. Migrate to gpt-3.5-turbo or "
#             "gpt-4o-mini via the Chat Completions API."
#         ),
#         source_url=_DEPRECATIONS_URL,
#         announced_at=datetime(2023, 7, 6, tzinfo=timezone.utc),
#         effective_at=datetime(2024, 1, 4, tzinfo=timezone.utc),
#         raw={"source": "seed", "replacement": "gpt-3.5-turbo"},
#     ),
# ]
