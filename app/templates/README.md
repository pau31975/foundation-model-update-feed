# `app/templates/` — Jinja2 Templates

This directory is the root for the Jinja2 template engine configured in `main.py`. FastAPI's `Jinja2Templates` instance points here and resolves template names relative to this directory.

---

## File map

| File | Role |
|---|---|
| `index.html` | The only template — full page UI for the feed |

---

## `index.html`

A server-rendered HTML page that displays the model lifecycle feed. It is returned by the `GET /` route in `main.py` via `TemplateResponse`.

### External resources loaded

| Resource | Purpose |
|---|---|
| Google Fonts — Inter 400/500/600/700 | Primary UI font (matches LiteLLM aesthetic) |
| `/static/styles.css` | All layout and component styles |
| `/static/app.js` | Async "Run collectors now" button handler |

### Page structure

```
<html>
 ├─ <head>                        Google Fonts preconnect + Inter link, stylesheet
 └─ <body>
     ├─ <header .site-header>     Logo, title, subtitle
     │
     ├─ <main .container>
     │   ├─ <section .filters-bar>
     │   │       <form method="get" action="/">
     │   │         Provider select   (populated from `providers`)
     │   │         Severity select   (populated from `severities`)
     │   │         Change type select(populated from `change_types`)
     │   │         Per page select   (25 / 50 / 100)
     │   │         [⚡ Major only] toggle link  (inside .filter-group--toggle)
     │   │         [Reset] link  (inside .filter-actions)
     │   │         Note: selects auto-submit via onchange — no Filter button
     │   │
     │   ├─ <section .actions-bar>
     │   │       Shows total item count
     │   │       [Run collectors now] button → calls triggerCollect()
     │   │       #collect-status div (hidden, shown by JS)
     │   │
     │   ├─ {% if items %}
     │   │   <ol .feed-list reversed>
     │   │     {% for item in items %}
     │   │       <li .feed-item .feed-item--{severity}>
     │   │         .feed-item__meta    provider badge, severity badge,
     │   │                             change-type badge,
     │   │                             announced_at <time> (if present, right-aligned)
     │   │         .feed-item__title   linked to source_url
     │   │         .feed-item__model   <code>model</code> + .feed-item__effective date
     │   │         .feed-item__summary paragraph
     │   │         .feed-item__footer  product name + .feed-item__source ("Source →" link)
     │   │     {% endfor %}
     │   │
     │   └─ {% else %}
     │       .empty-state           Instructions to run collectors
     │
     ├─ <footer .site-footer>      Links: /docs, /redoc, /api/updates, /health
     └─ <script src="/static/app.js">
```

---

## Jinja2 context variables

These are passed from the `GET /` route handler in `main.py` via `TemplateResponse("index.html", {...})`.

| Variable | Type | Description |
|---|---|---|
| `request` | `fastapi.Request` | Required by Jinja2Templates for URL generation |
| `items` | `list[ModelUpdateRead]` | Feed items to render (already filtered and paginated) |
| `total` | `int` | Total count of items matching the current filter (across all pages) |
| `limit` | `int` | Current "per page" value (25, 50, or 100) |
| `providers` | `list[str]` | All `Provider` enum values (`google`, `openai`, …) for the dropdown |
| `severities` | `list[str]` | All `Severity` enum values (`INFO`, `WARN`, `CRITICAL`) |
| `change_types` | `list[str]` | All `ChangeType` enum values |
| `selected_provider` | `str` | Currently active provider filter — empty string `""` when unset |
| `selected_severity` | `str` | Currently active severity filter — empty string `""` when unset |
| `selected_change_type` | `str` | Currently active change type filter — empty string `""` when unset |
| `major_only` | `bool` | Whether the Major only filter is active — passed back to preserve toggle state |

---

## Jinja2 filters used

| Filter | Applied to | Effect |
|---|---|---|
| `\| upper` | `item.provider` in badge | Forces provider name to uppercase (`OPENAI`, `GOOGLE`, …) |
| `\| lower` | `item.severity` in class name | Converts severity to lowercase for CSS class matching |
| `\| replace("_", " ") \| title` | `item.change_type` in badge, select options | Converts `DEPRECATION_ANNOUNCED` → `Deprecation Announced` |

---

## Filter form behaviour

The filter `<form>` uses `method="get"` and `action="/"`. There is **no explicit submit button** — every `<select>` has `onchange="this.form.submit()"` so the form is submitted instantly when a dropdown value changes. The `GET /` route reads params, passes them back in the context, and each `<option>` checks `{% if value == selected_value %}selected{% endif %}` to preserve dropdown state across page loads.

The **⚡ Major only** toggle is a link (`.filter-group--toggle`) rendered inside the filter bar but outside the form. It constructs its `href` by preserving the current provider/severity/change_type/limit params and toggling `major_only=true` / removing it. When active it uses `.btn-primary`; when inactive it uses `.btn-ghost`. Filtering to major-only shows only `NEW_MODEL`, `RETIREMENT`, and `DEPRECATION_ANNOUNCED` entries.

Clicking **Reset** navigates to `/` (no query parameters), showing all items with the default limit (50).

---

## Feed item rendering

Each `<li>` in the feed uses dynamic CSS classes to drive severity colouring:

```html
<li class="feed-item feed-item--{{ item.severity | lower }}">
```

This matches `.feed-item--critical`, `.feed-item--warn`, or `.feed-item--info` in `styles.css`, which sets the left border colour.

Provider badge class:

```html
<span class="badge badge--provider badge--{{ item.provider }}">
```

Matches `.badge--google`, `.badge--openai`, etc.

---

## Adding a new template

1. Create a new `.html` file in this directory (e.g. `detail.html`).
2. In `main.py`, add a route that returns `templates.TemplateResponse("detail.html", {"request": request, ...})`.
3. The template can extend a base layout if you create one (e.g. `_base.html` with `{% block content %}{% endblock %}`).
