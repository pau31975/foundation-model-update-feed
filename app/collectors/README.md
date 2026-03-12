# `app/collectors/` — Provider Collector Sub-package

Collectors are responsible for fetching, parsing, and returning structured model lifecycle events from each LLM provider's public documentation. Every collector extends `BaseCollector` and returns a list of `ModelUpdateCreate` objects. Deduplication happens downstream in the CRUD layer — collectors never touch the database directly.

---

## File map

| File | Role |
|---|---|
| `__init__.py` | Package marker, no logic |
| `base.py` | Abstract `BaseCollector` — shared HTTP client, retry logic, RSS helper |
| `gemini.py` | Google Gemini — RSS feed + HTML scraping (live) |
| `openai.py` | OpenAI — RSS feed + HTML scraping (live) |
| `anthropic.py` | Anthropic Claude — HTML scraping (live) |
| `azure.py` | Azure OpenAI — HTML scraping + seed fallback |
| `aws.py` | AWS Bedrock — RSS feed + HTML scraping + seed fallback |

---

## Collector status

| Provider | Data source(s) | Seed fallback? |
|---|---|---|
| Google Gemini | `blog.google/products/gemini/rss/` (RSS), `ai.google.dev/gemini-api/docs/deprecations`, `ai.google.dev/gemini-api/docs/changelog`, `cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions`, `cloud.google.com/vertex-ai/generative-ai/docs/release-notes` | No |
| OpenAI | `openai.com/blog/rss.xml` (RSS), `developers.openai.com/api/docs/deprecations`, `developers.openai.com/api/docs/changelog` | No |
| Anthropic | `platform.claude.com/docs/en/about-claude/models/all-models`, `platform.claude.com/docs/en/release-notes/overview` (`_collect_changelog`), `model-deprecations` (defined, not scraped) | No |
| Azure OpenAI | `…/azure/foundry-classic/openai/whats-new` (new), `…/azure/ai-services/openai/concepts/models`, legacy whats-new (defined, not scraped) | Yes — always appended |
| AWS Bedrock | `aws.amazon.com/about-aws/whats-new/recent/feed/` (RSS), `…/bedrock/…/model-lifecycle.html`, `…/bedrock/…/doc-history.html`, release-notes (defined, not scraped) | Yes — always appended |

> **Seeds (Azure & AWS only):** `_SEED_ENTRIES` are **always appended** unconditionally — not only when live fetches return nothing. DB fingerprint deduplication silently skips entries stored in a previous run.

---

## `base.py` — BaseCollector

All collectors inherit from `BaseCollector(ABC)`.

### Class members

| Member | Description |
|---|---|
| `provider_name: str` | Lowercase provider label set by each subclass |
| `__init__()` | Creates an `httpx.Client` with timeout, redirect following, and a descriptive `User-Agent` |
| `collect()` | **Abstract.** Must return `list[ModelUpdateCreate]`. Must not raise — catch all exceptions internally. |
| `_fetch(url)` | Shared HTTP GET helper. Retries up to `settings.collector_max_retries + 2` times. Returns the response body as a string, or `None` if all attempts fail. |
| `_fetch_rss(url)` | Fetches an RSS feed and returns a list of parsed entry dicts. Each dict has keys: `title`, `link`, `description`, `pub_date` (UTC `datetime` or `None`). Returns `[]` on any failure. Used by GeminiCollector, OpenAICollector, and AWSCollector. |
| `__del__()` | Closes the httpx client when the collector is garbage collected. |

### Retry behaviour

`_fetch` attempts the request in a loop. On `httpx.HTTPStatusError` (4xx / 5xx) or `httpx.RequestError` (network failure, timeout), it logs a warning and moves to the next attempt. After all attempts are exhausted it returns `None`. The limit is controlled by `settings.collector_max_retries` (default 2, so 4 total attempts).

---

## `gemini.py` — GeminiCollector

Scrapes the Google Gemini blog RSS feed and two documentation pages.

### Data sources

| URL | What it provides |
|---|---|
| `blog.google/products/gemini/rss/` | RSS feed — new model announcements and product updates |
| `ai.google.dev/gemini-api/docs/deprecations` | HTML table of deprecated/retired models with dates and replacements |
| `ai.google.dev/gemini-api/docs/changelog` | Dated changelog entries that mention new or changed models |
| `cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions` | HTML table of Vertex AI model versions with deprecation and retirement dates |
| `cloud.google.com/vertex-ai/generative-ai/docs/release-notes` | Dated release notes for Vertex AI generative AI — new models and capability changes |

### Parsing strategy — deprecations page

1. Fetch and parse HTML with `BeautifulSoup`.
2. Find all `<table>` elements.
3. For each table, read the header row to build a column index map (looks for keywords like "model", "deprecation", "shutdown", "replacement").
4. For each data row: extract model name, deprecation date, shutdown date, replacement.
5. Classify:
   - Shutdown date present → `ChangeType.RETIREMENT` / `Severity.CRITICAL`
   - Deprecation date only → `ChangeType.DEPRECATION_ANNOUNCED` / `Severity.WARN`

### Parsing strategy — changelog page

1. Walk all `<h2>` / `<h3>` headings; try to parse each as a date.
2. For headed sections, scan following sibling elements for text containing model-related keywords.
3. Extract Gemini model names with a regex.
4. Emit `ChangeType.NEW_MODEL` / `Severity.INFO` items.

### Parsing strategy — RSS feed

1. Call `self._fetch_rss(_RSS_URL)` to get the Google blog RSS entries.
2. Skip items with no Gemini model name and no AI/model keyword.
3. Classify each entry:
   - Keywords `deprecat`, `retire`, `shutdown`, `sunset`, `end-of-life` → `DEPRECATION_ANNOUNCED` / `WARN`.
   - `_RSS_RELEASE_RE` match (explicit release verbs: `introducing`, `launched`, `released`, `now available`, etc.) **or** `_RSS_MODEL_VERSION_RE` match (title starts with `ModelName VersionNumber`, e.g. `Gemini 3.1 Flash-Lite: …`) → `NEW_MODEL` / `INFO`.
   - Everything else → `CAPABILITY_CHANGED` / `INFO`.

`_RSS_MODEL_VERSION_RE` catches Google's common blog title pattern for model launches that don't use explicit release verbs.

### Parsing strategy — Vertex AI model versions page

1. Fetch and parse HTML with `BeautifulSoup`.
2. Find all `<table>` elements and skip any without a `model` column.
3. For each row, read model name, deprecation date, and unavailability/retirement date.
4. Classify:
   - Unavailability date present → `ChangeType.RETIREMENT` / `Severity.CRITICAL` (product `vertex_ai`)
   - Deprecation date only → `ChangeType.DEPRECATION_ANNOUNCED` / `Severity.WARN` (product `vertex_ai`)
   - Neither date → row skipped (informational only)

### Parsing strategy — Vertex AI release notes page

1. Walk all `<h2>` / `<h3>` headings; try to parse each as a date.
2. For headed sections, scan following sibling elements for text containing Gemini model or Vertex AI keywords.
3. Extract Gemini model names with a regex.
4. Classify using the same keyword heuristics as the RSS feed (`deprecat`/`retire` → `DEPRECATION_ANNOUNCED`; explicit release verbs or model version pattern → `NEW_MODEL`; otherwise `CAPABILITY_CHANGED`).
5. Emitted items use product `vertex_ai`.

### `_parse_date` helper

Tries these formats in order: `%B %d, %Y`, `%b %d, %Y`, `%Y-%m-%d`, `%d %B %Y`, `%d %b %Y`, then falls back to a `YYYY-MM-DD` regex extraction. Always returns a timezone-aware UTC `datetime` or `None`.

---

## `openai.py` — OpenAICollector

Scrapes the OpenAI blog RSS feed and the OpenAI deprecations/changelog documentation pages.

### Data sources

| URL | What it provides |
|---|---|
| `openai.com/blog/rss.xml` | RSS feed (follows redirects) — model releases, feature launches, deprecation notices |
| `developers.openai.com/api/docs/deprecations` | HTML page listing all deprecated/retired models with dates |
| `developers.openai.com/api/docs/changelog` | Dated changelog entries for new model and capability releases |

### Parsing strategy — RSS feed

1. Call `self._fetch_rss(_RSS_URL)` to get the OpenAI news RSS entries.
2. **Skip entries where `_RSS_MODEL_RE` finds no specific model name** — unlike Gemini, OpenAI only processes entries that mention a recognised model identifier (gpt-4, gpt-5, o1–o4, dall-e, sora, whisper, text-embedding, tts).
3. Classify each (model-matching) entry:
   - Keywords `deprecat`, `retire`, `shutdown`, `sunset`, `end-of-life` → `DEPRECATION_ANNOUNCED` / `WARN`.
   - `_RSS_RELEASE_RE` match (explicit release verbs) → `NEW_MODEL` / `INFO`.
   - Everything else → `CAPABILITY_CHANGED` / `INFO`.

### Parsing strategy — deprecations page

Two strategies are tried in order; the first that returns results is used.

**Strategy 1 — definition lists (`<dl>/<dt>/<dd>`):**
1. Find `<dl>` elements. Each `<dt>` is a model name; the paired `<dd>` has the description text.
2. Extract shutdown / deprecation dates from description text using regex patterns (`shutdown on <date>`, `deprecated on/as of <date>`).
3. Classify: shutdown date → `RETIREMENT`; deprecation date only → `DEPRECATION_ANNOUNCED`.

**Strategy 2 — headings fallback:**
1. Find all `<h2>` / `<h3>` headings.
2. Aggregate text of following sibling elements until the next heading.
3. Filter to sections containing deprecation-related keywords.
4. Extract model ID patterns (gpt-, text-, davinci, whisper, dall-e, embedding, tts) via regex.
5. Same date extraction and classification logic as Strategy 1.

---

## `anthropic.py` — AnthropicCollector

Scrapes two Anthropic documentation pages. There is no seed fallback — if fetches fail, the collector returns an empty list.

### Data sources

| URL | What it provides | Actively scraped? |
|---|---|---|
| `platform.claude.com/docs/en/about-claude/models/all-models` | Model table with status; deprecated/retired rows contain end-of-support info | **Yes** (`_collect_models_page`) |
| `platform.claude.com/docs/en/release-notes/overview` | Dated changelog entries (h3 date headings) with new model announcements | **Yes** (`_collect_changelog`) |
| `platform.claude.com/docs/en/about-claude/model-deprecations` | Deprecation schedule — configured as `anthropic_source_urls[2]` | No — defined but not called |

> **`_CHANGELOG_URL`** is hardcoded to `platform.claude.com/docs/en/release-notes/overview` (the final redirect target) rather than using the config value `anthropic_source_urls[1]` (`/release-notes/api`), which redirects to a different page with no live changelog data.

### Parsing strategy — models page

1. Find all `<table>` elements; skip tables with no model-related header keywords.
2. Build a column index map for `model`, `status`, `deprecation`, `replacement` columns.
3. For each row, check cell text for keywords: `deprecated`, `legacy`, `end of support`, `retired`, `sunset`.
4. Classify: `retired` / `end of support` → `RETIREMENT` / `CRITICAL`; otherwise `DEPRECATION_ANNOUNCED` / `WARN`.

### Parsing strategy — release notes (changelog)

1. Walk `<h2>`/`<h3>`/`<h4>` headings; try to parse each heading as a date (e.g. `"February 17, 2026"`).
2. Aggregate sibling text until the next heading of the same level.
3. Filter sections mentioning Claude model names or keywords (`new model`, `launch`, `available`).
4. Extract model name via `claude[-\s][\w\-\.]+` regex.
5. Classify as `DEPRECATION_ANNOUNCED` (has retire/deprecate keywords) or `NEW_MODEL`.

---

## `azure.py` — AzureCollector

Scrapes two Microsoft Azure documentation pages. Seeds are always appended on top of live results.

### Data sources

| URL | What it provides | Actively scraped? |
|---|---|---|
| `learn.microsoft.com/…/azure/foundry-classic/openai/whats-new` | New canonical What's New page with retirement announcements and GA notices | **Yes** (`_collect_whats_new`) |
| `learn.microsoft.com/…/azure/ai-services/openai/concepts/models` | Model availability tables and retirement/deprecation sections | **Yes** (`_collect_models_page`) |
| `learn.microsoft.com/…/azure/ai-services/openai/whats-new` | Legacy What's New page (historical entries) — configured as `azure_source_urls[2]` | No — defined but not called |

### Parsing strategy — models page

**Strategy 1 – heading-scoped tables:**
1. Find headings (`<h2>`–`<h5>`) whose text matches retirement/deprecation keywords.
2. Collect `<table>` elements that are siblings of that heading (until the next same-level heading).
3. Parse each table with `_parse_retirement_table()`.

**Strategy 2 – header-keyword scan:**
1. Scan every `<table>` on the page; check if the header row mentions `retire`, `deprecat`, `sunset`, or `end of life`.
2. Parse matching tables with `_parse_retirement_table()`.

`_parse_retirement_table()` maps columns to `model`, `retirement` (date string), and `replacement`, then classifies rows as `RETIREMENT` / `CRITICAL` (retirement date present) or `DEPRECATION_ANNOUNCED` / `WARN`.

### Parsing strategy — What's New page

1. Walk `<h2>`/`<h3>`/`<h4>` headings; try to parse each as a date.
2. Aggregate sibling text; filter to sections mentioning model-related keywords.
3. Extract Azure/OpenAI model name patterns (gpt-, o1/o3, dall-e, whisper, embeddings, tts).
4. Classify as `DEPRECATION_ANNOUNCED` (retire/deprecate keywords) or `NEW_MODEL` (available/launch/GA).

---

## `aws.py` — AWSCollector

Scrapes the AWS What's New RSS feed and two documentation pages. Seeds are always appended on top of live results.

### Data sources

| URL | What it provides | Actively scraped? |
|---|---|---|
| `aws.amazon.com/about-aws/whats-new/recent/feed/` | RSS feed — Bedrock model launches, service announcements | **Yes** (`_collect_rss`) |
| `docs.aws.amazon.com/bedrock/…/model-lifecycle.html` | Lifecycle table: model ID, status, deprecation date, end-of-support date | **Yes** (`_collect_lifecycle_page`) |
| `docs.aws.amazon.com/bedrock/…/doc-history.html` | Changelog table: change description + date, used for new model announcements | **Yes** (`_collect_doc_history`) |
| `docs.aws.amazon.com/bedrock/…/release-notes.html` | Full release notes — configured as `aws_source_urls[2]` | No — defined but not called |

### Parsing strategy — RSS feed

1. Call `self._fetch_rss(_RSS_URL)` to get AWS What's New entries.
2. Skip entries where neither `_AWS_BEDROCK_RE` finds a Bedrock keyword nor "bedrock" appears in the title.
3. Classify each entry:
   - Keywords `deprecat`, `retire`, `end-of-support`, `end-of-life`, `sunset` → `DEPRECATION_ANNOUNCED` / `WARN`.
   - `_RSS_MODEL_RE` match with `_RSS_RELEASE_RE` verb → `NEW_MODEL` / `INFO`.
   - Everything else → `CAPABILITY_CHANGED` / `INFO`.

### Parsing strategy — lifecycle page

1. Find all `<table>` elements with a header row containing `model` or `model id`.
2. Build column map for `model`, `status`, `deprecation`, `end_of_support`, `replacement`.
3. Skip rows with no deprecation/retirement signal.
4. Classify: end-of-support date present → `RETIREMENT` / `CRITICAL`; deprecation date only → `DEPRECATION_ANNOUNCED` / `WARN`; no date but keyword present → `DEPRECATION_ANNOUNCED` / `WARN`.

### Parsing strategy — doc history

1. Find all tables and map columns to `change`, `description`, `date`.
2. Filter rows mentioning Bedrock model providers (Claude, Llama, Titan, Nova, Mistral, Cohere, AI21, Jurassic).
3. Further filter to rows mentioning `new`, `added`, `launch`, `available`, `deprecat`, `retire`, or `support`.
4. Extract model IDs matching provider-prefixed patterns (`anthropic.*`, `amazon.*`, `meta.*`, etc.).
5. Classify as `DEPRECATION_ANNOUNCED` or `NEW_MODEL` based on keywords.

---

## Implementing a new collector

### 1. Create the collector file

```python
# app/collectors/myprovider.py
import logging
from app.collectors.base import BaseCollector
from app.schemas import ChangeType, ModelUpdateCreate, Provider, Severity

logger = logging.getLogger(__name__)

_RSS_URL = "https://provider.com/blog/rss.xml"
_SOURCE_URL = "https://provider.com/docs/changelog"

class MyProviderCollector(BaseCollector):
    provider_name = "myprovider"

    def collect(self) -> list[ModelUpdateCreate]:
        items: list[ModelUpdateCreate] = []
        items.extend(self._collect_rss())
        items.extend(self._collect_changelog())
        return items

    def _collect_rss(self) -> list[ModelUpdateCreate]:
        results: list[ModelUpdateCreate] = []
        for entry in self._fetch_rss(_RSS_URL):
            # classify & build ModelUpdateCreate from entry ...
            pass
        return results

    def _collect_changelog(self) -> list[ModelUpdateCreate]:
        html = self._fetch(_SOURCE_URL)
        if not html:
            return []   # return empty list on failure — no seed fallback
        items: list[ModelUpdateCreate] = []
        # parse html with BeautifulSoup ...
        return items
```

> **No seed fallback needed.** Return an empty list `[]` when a fetch fails. The DB fingerprint deduplication layer handles subsequent successful fetches with no duplicates.

### 2. Register in the collector service

Open `app/services/collector_service.py` and add the new class to `_ALL_COLLECTORS`:

```python
from app.collectors.myprovider import MyProviderCollector

_ALL_COLLECTORS = [
    GeminiCollector,
    OpenAICollector,
    MyProviderCollector,   # ← add here
    ...
]
```

### 3. Add the provider enum value

If it is a brand-new provider, add it to `Provider` in `app/schemas.py`:

```python
class Provider(str, Enum):
    myprovider = "myprovider"
```

And add its badge colour to `app/static/styles.css`:

```css
.badge--myprovider { background: #f0f0ff; color: #5050ff; border: 1px solid #c0c0ff; }
```

---

## Collector call flow

```
POST /api/collect
      │
      ▼
collector_service.run_all_collectors(db)
      │
      ├─ instantiate MyCollector()         ← __init__ creates httpx.Client
      │
      ├─ items = MyCollector.collect()
      │       ├─ self._fetch_rss(rss_url)  ← HTTP GET → RSS XML parse
      │       │     └─ returns list[entry dict]
      │       └─ self._fetch(url)          ← HTTP GET with retries
      │             └─ BeautifulSoup parse
      │             └─ returns list[ModelUpdateCreate]
      │
      └─ for item in items:
              crud.create_update(db, item)
                   └─ INSERT … ON CONFLICT fingerprint → None (skip)
```

---

## Severity classification

Severity is a three-level enum (`INFO` → `WARN` → `CRITICAL`). Each collector derives it from the `ChangeType` of the event, consistently across all providers:

| `ChangeType` | `Severity` | Meaning |
|---|---|---|
| `RETIREMENT` | **CRITICAL** | A hard end-of-support / shutdown / retirement date is known |
| `DEPRECATION_ANNOUNCED` | **WARN** | Model is deprecated but no hard shutdown date confirmed yet |
| `NEW_MODEL` / `CAPABILITY_CHANGED` | **INFO** | New launch, GA announcement, or capability update |

### Per-provider rules

#### Google Gemini (`gemini.py`)

| Condition | `ChangeType` | `Severity` |
|---|---|---|
| RSS title: `_RSS_RELEASE_RE` or `_RSS_MODEL_VERSION_RE` match | `NEW_MODEL` | **INFO** |
| RSS text: `deprecat`, `retire`, `shutdown`, `sunset`, `end-of-life` | `DEPRECATION_ANNOUNCED` | **WARN** |
| RSS entry (no release/deprecation signal) | `CAPABILITY_CHANGED` | **INFO** |
| HTML deprecation page: `shutdown_date` column has a value | `RETIREMENT` | **CRITICAL** |
| HTML deprecation page: only `deprecation_date` present | `DEPRECATION_ANNOUNCED` | **WARN** |
| HTML changelog entry | `NEW_MODEL` | **INFO** |

#### OpenAI (`openai.py`)

| Condition | `ChangeType` | `Severity` |
|---|---|---|
| RSS entry where `_RSS_RELEASE_RE` matches + model name found | `NEW_MODEL` | **INFO** |
| RSS entry with deprecation keywords + model name found | `DEPRECATION_ANNOUNCED` | **WARN** |
| RSS entry with model name but no clear signal | `CAPABILITY_CHANGED` | **INFO** |
| RSS entry with no model name (`_RSS_MODEL_RE` no match) | *(skipped entirely)* | — |
| HTML deprecation page: shutdown date present | `RETIREMENT` | **CRITICAL** |
| HTML deprecation page: deprecation date only | `DEPRECATION_ANNOUNCED` | **WARN** |
| HTML changelog entry | `NEW_MODEL` | **INFO** |

> OpenAI centralises this logic in a `_classify_severity(change_type)` helper function.

#### Anthropic (`anthropic.py`)

| Condition | `ChangeType` | `Severity` |
|---|---|---|
| Models page: row text contains `retired` or `end of support` | `RETIREMENT` | **CRITICAL** |
| Models page: row text contains `deprecated`, `legacy`, `sunset` | `DEPRECATION_ANNOUNCED` | **WARN** |
| Changelog entry with deprecation/retirement keywords | `DEPRECATION_ANNOUNCED` | **WARN** |
| Changelog entry (no deprecation signal) | `NEW_MODEL` | **INFO** |

#### Azure OpenAI (`azure.py`)

| Condition | `ChangeType` | `Severity` |
|---|---|---|
| `retirement_date` column has a value | `RETIREMENT` | **CRITICAL** |
| Deprecation/retirement text but no date | `DEPRECATION_ANNOUNCED` | **WARN** |
| What's New entry with retire/deprecate keywords | `DEPRECATION_ANNOUNCED` | **WARN** |
| What's New entry with available/launch/GA keywords | `NEW_MODEL` | **INFO** |

#### AWS Bedrock (`aws.py`)

| Condition | `ChangeType` | `Severity` |
|---|---|---|
| `end_of_support_date` column has a value | `RETIREMENT` | **CRITICAL** |
| Only `deprecation_date` present | `DEPRECATION_ANNOUNCED` | **WARN** |
| Deprecated row with no date at all | `DEPRECATION_ANNOUNCED` | **WARN** |
| Doc-history entry with new-model keywords | `NEW_MODEL` | **INFO** |
