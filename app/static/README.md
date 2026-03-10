# `app/static/` — Frontend Static Assets

This directory contains the two client-side files that FastAPI serves under the `/static/` URL prefix via `StaticFiles`. There are no build steps, bundlers, or JavaScript frameworks — the UI is server-rendered HTML with a small CSS file and a minimal JS enhancement.

---

## File map

| File | Role |
|---|---|
| `styles.css` | Full application stylesheet — layout, components, design tokens |
| `app.js` | Single async function for the "Run collectors now" button |

---

## `styles.css`

### Design system

The stylesheet is written in plain CSS (no preprocessor) and follows the **Tremor + Tailwind** design language used by [LiteLLM](https://github.com/BerriAI/litellm). It is a **light-mode** design.

**Font:** [Inter](https://fonts.google.com/specimen/Inter) (400 / 500 / 600 / 700), loaded from Google Fonts in `templates/index.html`. System-font fallbacks are included in `--font-sans` for offline use.

### CSS custom properties (`:root`)

All design tokens are declared as CSS variables so the entire palette can be changed in one place.

**Backgrounds**
| Variable | Value | Usage |
|---|---|---|
| `--bg` | `#f9fafb` (gray-50) | Page body background |
| `--bg-card` | `#ffffff` | Card / panel surfaces |
| `--bg-card-border` | `#e5e7eb` (gray-200) | Card borders, dividers |

**Text**
| Variable | Value | Usage |
|---|---|---|
| `--text` | `#111827` (gray-900) | Primary / strong text |
| `--text-muted` | `#6b7280` (gray-500) | Labels, secondary info |
| `--text-subtle` | `#9ca3af` (gray-400) | Timestamps, footer text |

**Accent (brand colour — indigo)**
| Variable | Value | Usage |
|---|---|---|
| `--accent` | `#6366f1` (indigo-500) | Links, primary buttons, focus rings |
| `--accent-hover` | `#4f46e5` (indigo-600) | Hover states |
| `--accent-faint` | `#eef2ff` (indigo-50) | Subtle accent backgrounds |

**Severity colours**
| Variable | Value | Meaning |
|---|---|---|
| `--clr-critical` | `#dc2626` (red-600) | Retirements, outages |
| `--clr-warn` | `#d97706` (amber-600) | Deprecation announcements |
| `--clr-info` | `#2563eb` (blue-600) | New models, capability changes |

**Provider colours** (used in badges)
| Variable | Provider |
|---|---|
| `--clr-google` | green-600 |
| `--clr-openai` | emerald-600 |
| `--clr-anthropic` | orange-700 |
| `--clr-azure` | sky-700 |
| `--clr-aws` | amber-700 |

**Spacing and shape**
| Variable | Value | Usage |
|---|---|---|
| `--radius` | `8px` | Border radius for all components |
| `--shadow-card` | layered `0 1px 3px …` | Card resting shadow |
| `--shadow-input` | `0 1px 2px …` | Inputs and buttons |

### Component classes

| Class | Description |
|---|---|
| `.container` | Centred content wrapper, `max-width: 900px` |
| `.site-header` | White top bar with bottom border; contains the `<h1>` and subtitle |
| `.filters-bar` | White card containing the filter `<form>` |
| `.filter-group` | Vertical `label + select` unit with uppercased label |
| `.actions-bar` / `.actions-row` | Space-between row with item count and button |
| `.collect-status` | Inline status message below the action row |
| `.collect-status--ok` | Green variant (success) |
| `.collect-status--error` | Red variant (failure) |
| `.btn` | Base button style |
| `.btn-primary` | Indigo filled — primary action |
| `.btn-secondary` | White outlined — secondary action |
| `.btn-ghost` | Transparent — reset / tertiary |
| `.feed-list` | Flex column list of feed cards |
| `.feed-item` | Individual card with coloured left border |
| `.feed-item--critical` | Red left border |
| `.feed-item--warn` | Amber left border |
| `.feed-item--info` | Blue left border |
| `.feed-item__meta` | Badge row at the top of each card |
| `.feed-item__time` | Timestamp, right-aligned in meta row |
| `.feed-item__title` | `<h2>` with link to source |
| `.feed-item__model` | `<code>` model ID + effective date |
| `.feed-item__summary` | Body paragraph |
| `.feed-item__footer` | Footer row with product, announced date, source link |
| `.badge` | Pill badge base (full border-radius) |
| `.badge--{provider}` | Provider-specific colour (google, openai, anthropic, azure, aws) |
| `.badge--{severity}` | Severity colour (critical, warn, info) |
| `.badge--change-type` | Neutral gray for change type label |
| `.empty-state` | Centred placeholder shown when no items match the filters |
| `.site-footer` | White bottom bar with doc links |

### Responsive breakpoint

A single `@media (max-width: 640px)` rule stacks the filter row, footer nav, and actions row vertically on small screens.

---

## `app.js`

Contains one function: `triggerCollect()`, called by the **Run collectors now** button's `onclick` attribute.

### What it does

1. Disables the button and shows a loading message in `#collect-status`.
2. `POST /api/collect` (no body).
3. Parses the JSON response:
   - **Success:** Displays `"Done — added X, skipped Y duplicate(s)."` with a green status style. If any items were added, reloads the page after 1.2 seconds so the new feed entries appear.
   - **HTTP error:** Displays the status code and error body with a red status style.
   - **Network error:** Displays the JavaScript error message with a red status style.
4. Always re-enables the button in the `finally` block.

### How it is loaded

`templates/index.html` includes `<script src="/static/app.js"></script>` at the bottom of `<body>` (after the DOM is parsed).

### CSS classes used by `app.js`

| Class | Applied when |
|---|---|
| `.collect-status` | Always (base style, set in HTML) |
| `.collect-status--ok` | On successful collect |
| `.collect-status--error` | On HTTP or network error |

---

## Serving

FastAPI mounts this directory in `main.py`:

```python
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
```

Files are served directly without any transformation. The browser caches them using standard HTTP headers provided by Starlette's `StaticFiles`.
