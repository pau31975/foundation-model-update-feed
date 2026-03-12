# app/static/

Frontend assets served at `/static/`.

---

## `app.js`

Minimal client-side script — handles only the "Run collectors now" button.

**`triggerCollect()`**
1. Disables the button and shows "Contacting providers, please wait…".
2. Sends `POST /api/collect` via `fetch()`.
3. On success: displays "Done — added X, skipped Y duplicate(s)." Reloads the page after 1.2 s if any items were added.
4. On HTTP error or network failure: displays the error message inline.
5. Always re-enables the button in `finally`.

---

## `styles.css`

All application styles using CSS custom properties (no build step required).

**Design tokens (CSS variables):**

| Group | Variables |
|-------|-----------|
| Layout | `--bg`, `--surface`, `--border`, `--radius` |
| Text | `--text-primary`, `--text-secondary`, `--text-muted` |
| Accent | `--accent` (indigo-500) |
| Severity | `--clr-critical` (red), `--clr-warn` (amber), `--clr-info` (blue) |
| Providers | `--clr-google`, `--clr-openai`, `--clr-anthropic`, `--clr-azure`, `--clr-aws` |

**Key component classes:**

| Class | Purpose |
|-------|---------|
| `.badge--<provider>` | Colored provider chip |
| `.badge--<severity>` | Colored severity chip |
| `.badge--<change_type>` | Change-type label |
| `.feed-card` | Event card with Tremor-style box shadow |
| `.collect-status--ok` | Green inline success feedback |
| `.collect-status--error` | Red inline error feedback |

Font: Inter (Google Fonts) with monospace fallback for `<code>` elements. Max-width container: 900 px.
