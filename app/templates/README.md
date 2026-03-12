# app/templates/

Jinja2 server-rendered templates for the web UI. Served by `GET /`.

---

## `index.html`

Single-page feed UI. Rendered with context variables injected by `main.py`:

| Variable | Description |
|----------|-------------|
| `items` | List of `ModelUpdateRead` objects |
| `total` | Total matching item count |
| `providers` / `severities` / `change_types` | Dropdown option lists |
| `selected_*` | Currently active filter values |
| `limit` | Current page size |
| `next_cursor` | Cursor for the next page |

### Sections

**Header**
Title and subtitle banner.

**Filter bar**
`<form method="get" action="/">` with `<select>` dropdowns for provider, severity, change type, and limit. Auto-submits on change via `onchange="this.form.submit()"`. A reset link clears all filters.

**Actions bar**
Displays total item count. "Run collectors now" button calls `triggerCollect()` in `app.js`.

**Feed list**
`<ol class="feed-list" reversed>` of event cards. Each card shows:
- Provider, severity, and change-type badges
- Announced date
- Title linked to `source_url`
- Model `<code>` tag (when present)
- Effective date (when present)
- Summary text
- Product label and source link

**Empty state**
Shown when no items match the current filters. Links to `/docs` for manual item creation.

**Footer**
Links to `/docs`, `/redoc`, `/api/updates`, and `/health`.

### Filtering (major events only)
The default `GET /` view passes `major_only=True` to `list_updates`, restricting results to `NEW_MODEL`, `RETIREMENT`, and `DEPRECATION_ANNOUNCED` events. All change types are accessible via the API (`GET /api/updates`).
