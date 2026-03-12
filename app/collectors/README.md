# app/collectors/

Per-provider scrapers that collect AI model lifecycle events from documentation pages and RSS feeds. Each collector is a subclass of `BaseCollector`.

---

## BaseCollector (`base.py`)

Abstract base class providing shared HTTP and parsing utilities.

| Method | Description |
|--------|-------------|
| `collect()` | **Abstract.** Returns `list[ModelUpdateCreate]`. Must never raise. |
| `_fetch(url)` | HTTP GET with retry loop (up to `COLLECTOR_MAX_RETRIES + 1` attempts). Returns `str \| None`. |
| `_fetch_rss(url)` | Fetches and parses RSS XML via `xml.etree.ElementTree`. Returns `list[dict]` with keys `title`, `link`, `description`, `pub_date`. |
| `_map_columns(headers)` | Maps table header text to column indices for dynamic HTML table parsing. |

Uses `httpx.Client` with a custom `User-Agent` and configurable timeout from `config.py`.

---

## Collectors

### `gemini.py` — `GeminiCollector` (`provider = "google"`)

Most comprehensive collector with five parsing strategies:

| Method | Source |
|--------|--------|
| `_collect_rss()` | Google Gemini blog RSS — filters by `gemini-*` model name patterns |
| `_collect_deprecations()` | Gemini API deprecations table |
| `_collect_changelog()` | Gemini API changelog |
| `_collect_vertex_model_versions()` | Vertex AI model versions page |
| `_collect_vertex_release_notes()` | Vertex AI release notes |

---

### `openai.py` — `OpenAICollector` (`provider = "openai"`)

| Method | Source |
|--------|--------|
| `_collect_rss()` | OpenAI blog RSS — filters by `_RSS_MODEL_RE` (gpt-4/5, o1–o4, DALL-E, Sora, Whisper, etc.) |
| `_collect_deprecations()` | Deprecations page (`<dl>` lists and tables) |
| `_collect_changelog()` | Changelog page for new model announcements |

Classifies RSS entries as `NEW_MODEL`, `DEPRECATION_ANNOUNCED`, or `CAPABILITY_CHANGED` based on title keywords.

---

### `anthropic.py` — `AnthropicCollector` (`provider = "anthropic"`)

| Method | Source |
|--------|--------|
| `_collect_models_page()` | All-models HTML table; detects `deprecat`, `legacy`, `retired`, `sunset` keywords per row |
| `_collect_changelog()` | API release notes page |

Emits `RETIREMENT`/`CRITICAL` or `DEPRECATION_ANNOUNCED`/`WARN` based on keyword match. `_parse_date()` tries 7+ format strings with ISO regex fallback.

---

### `azure.py` — `AzureCollector` (`provider = "azure"`)

| Method | Source |
|--------|--------|
| `_collect_models_page()` | Two-strategy HTML parser: heading keywords + retirement table headers |
| `_collect_whats_new()` | Canonical and legacy What's New pages |

No RSS feed available. Uses `_parse_retirement_table()` for structured table extraction.

---

### `aws.py` — `AWSCollector` (`provider = "aws"`)

| Method | Source |
|--------|--------|
| `_collect_rss()` | AWS What's New RSS — filtered by `_AWS_BEDROCK_RE` (Nova, Titan, Llama, Mistral, Cohere Command, etc.) |
| `_collect_lifecycle_page()` | Model lifecycle table (ID, status, deprecation date, end-of-support date) |
| `_collect_doc_history()` | Doc history changelog for new model announcements |

---

## Adding a New Provider

1. Create `app/collectors/<provider>.py` subclassing `BaseCollector`.
2. Set `provider_name = "<provider>"`.
3. Implement `collect() -> list[ModelUpdateCreate]` — catch all exceptions internally.
4. Append the class to `_ALL_COLLECTORS` in [`app/services/collector_service.py`](../services/README.md).
