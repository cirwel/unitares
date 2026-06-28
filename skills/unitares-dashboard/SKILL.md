---
name: unitares-dashboard
description: >
  Use when adding, editing, or reviewing sections on the unitares dashboard
  (dashboard/redesign/). Captures the redesign's conventions: the section-module
  pattern (window.X = { load }), the live-or-snapshot data seam, theme-aware
  charts via design tokens, and the app.html wiring (nav / pane / lazyLoad /
  RELOAD / retheme). A repo-specific reference — not general dashboard advice.
last_verified: "2026-06-28"
freshness_days: 30
source_files:
  - unitares/dashboard/redesign/app.html
  - unitares/dashboard/redesign/data.js
  - unitares/dashboard/redesign/snapshot.js
  - unitares/dashboard/redesign/sections/eisv.js
  - unitares/dashboard/redesign/sections/metrics.js
  - unitares/src/http_api.py
---

# Adding a Section to the UNITARES Dashboard (redesign)

## Orientation

The live dashboard is **`dashboard/redesign/`** — buildless (raw HTML/CSS/JS,
no framework, no bundle), served by `http_dashboard_redesign` in
`src/http_api.py` at `/` and `/dashboard`. The classic dashboard and its
allowlist / script-load-chain / `vite` build were **retired** — ignore any
older guidance about `index.html`, `allowed_files`, `MetricColors`, or
`Chart.defaults`. There is **no allowlist** (a directory resolver serves
`redesign/**`) and **no restart** needed (files are read per request).

A "section" is one nav tab. Each is a self-contained module that renders into
its own mount and is wired in `app.html`.

## The section-module pattern

A section is an IIFE that attaches `window.<Name> = { load }` (add `retheme`
if it draws charts; add `applyEvent` / `notifyNew` if it's a live surface — see
**Live-surface hooks** below). `load()` renders into its mount on first call and
updates **in place** on subsequent calls (so the 10s auto-refresh doesn't flicker
or reset form state). Worked references: `sections/eisv.js` (charts, retheme,
`applyEvent`) and `sections/metrics.js` (charts + a picker that survives
auto-refresh).

## Integration checklist (all in `dashboard/redesign/`)

| # | Do | File |
|---|----|----|
| 1 | Create the module `sections/NAME.js` → `window.NAME = { load[, retheme] }` | `sections/NAME.js` |
| 2 | Add a data accessor returning `{source, data}` via `withFallback(liveFn, snapFn)` | `data.js` |
| 3 | Add a snapshot mock so the section renders offline/portably | `snapshot.js` |
| 4 | Nav link `<a href="#NAME" data-section="NAME">Name</a>` | `app.html` (nav) |
| 5 | Pane `<section class="section" data-pane="NAME" hidden>` with `<div id="NAME-mount">` | `app.html` (main) |
| 6 | `<script src="./sections/NAME.js"></script>` (order-independent — modules self-init via `load`) | `app.html` |
| 7 | `lazyLoad` entry: `if (id === "NAME" && window.NAME) { loaded[id]=true; window.NAME.load(); }` | `app.html` |
| 8 | If it's a live monitor view, add to `RELOAD` (10s tick) | `app.html` |
| 9 | If it draws charts, call `window.NAME.retheme()` in the theme-toggle handler | `app.html` |
| 10 | (live surface) Expose `applyEvent(msg)` → truthy when handled in place; register `APPLY.NAME = (msg) => window.NAME?.applyEvent?.(msg)` | `sections/NAME.js`, `app.html` |
| 11 | (badge) Expose `notifyNew()`; register `NOTIFY.<event_type> = () => window.NAME?.notifyNew?.()` | `sections/NAME.js`, `app.html` |

## Data seam — live-or-snapshot (Item 2)

Views never call `fetch` directly. They `await DATA.x()`, which returns
`{ source: "live" | "snapshot", data }`. The accessor tries the live endpoint
(`authFetch` for REST, `callTool` for `/v1/tools/call`) and falls back to the
bundled `SNAPSHOT` on any failure, so the dashboard renders portably (opened as
a file, cross-origin, or server down). Badge freshness in the view with
`<span class="src-badge ${source}">${source}</span>`.

```js
async metricsCatalog() {
  return withFallback(
    async () => { const j = await authFetch("/v1/metrics/catalog");
                  return j && Array.isArray(j.metrics) ? j.metrics : null; },
    () => S().metrics.catalog,   // snapshot fallback
  );
}
```

Returning `null`/empty from `liveFn` triggers the snapshot fallback. For
headline cards where a stale snapshot under a "live" badge would mislead, prefer
returning `null` per-field and rendering "—" (see `data.js::stats`).

## Theme-aware charts (Item 9) — the real chart trap here

Chart.js still fights the theme, but the redesign fix is **design tokens**, not
hardcoded hex. Read colours from CSS custom properties so the chart re-renders
correctly in both `ink` (dark) and `paper` (light):

```js
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const tick = cssVar("--muted"), grid = rgba(cssVar("--ink"), 0.06), line = cssVar("--accent");
```

Then expose `retheme()` that rebuilds the chart from cached data (token values
change on toggle), and call it from the theme handler in `app.html`. Copy the
option shape from `sections/eisv.js::baseOptions`.

**No date adapter.** `app.html` loads `chart.umd` from CDN **without**
`chartjs-adapter-date-fns`, so `type: "time"` scales will not work. Use a
**category** x-axis with pre-formatted labels (e.g. `MM-DD`) — see
`sections/metrics.js::fmtLabel`.

## Auto-refresh discipline (Item 8)

`RELOAD[id]` fires every ~10s while the section is active, the tab is visible,
and no input is focused. So `load()` must: update charts/data **in place**
(don't `new Chart()` every tick), and not rebuild a `<select>`/`<input>` the
operator is using. The global tick already skips while an input is focused, so
repopulating a closed dropdown on refresh is safe — but preserve the current
selection. `sections/metrics.js` shows the first-render-then-update pattern.

## Live-surface hooks (WS diff-push) — #1164

The WS connection is a **hybrid**, not pure doorbell→refetch: a section can apply
a live event in place instead of refetching. `onWsEvent(msg)` in `app.html`
dispatches each incoming event through two maps:

- **`NOTIFY[msg.type]()`** fires first, regardless of the active section — for
  "N new since you loaded" badges that must not auto-refetch
  (`NOTIFY.knowledge_write = () => window.Discoveries?.notifyNew?.()`).
- **`APPLY[activeSection](msg)`** then runs the *active* section's live handler.
  If the section exposes `applyEvent(msg)` and it returns **truthy** ("handled
  live"), `onWsEvent` returns early and **suppresses** the debounced
  `refreshActive()` refetch. Return falsy / omit `applyEvent` to fall through to
  the doorbell→refetch fallback (~1500ms debounce). Register as
  `APPLY.NAME = (msg) => window.NAME?.applyEvent?.(msg)`.

So the model is **notify → apply-in-place → else doorbell-refetch**. `applyEvent`
updates **in place** (same discipline as `load()`) and must not fabricate state
the event doesn't carry. Worked references: `sections/eisv.js::applyEvent`
(appends a push, re-buckets via `updateInPlace`), `sections/landing.js`
(composite status snaps on check-in), `sections/discoveries.js` (`notifyNew`
badge). The WS plumbing lives in `ws.js`.

## Read-only today

The redesign sends only the read bearer token. Operator **write** actions
(archive/resume agent, request review, discovery status) are **not** wired —
they need the `X-Unitares-Operator` header under `STRICT_IDENTITY` (`PLAN.md`).
Don't assume a section can mutate state; if you add a write surface, that's a
deliberate new capability, not a copy-paste.

## Verify before claiming done

The dashboard is buildless, so the gate is lint plus a cheap logic check:

1. `cd dashboard && npm run lint` — must be 0 errors (warnings allowed).
2. Headless logic drive (optional, fast): load `snapshot.js` + `data.js` +
   `sections/NAME.js` in a jsdom stub with `window.Chart` stubbed and call
   `NAME.load()`; assert the mount populated and (for charts) the paint path
   ran. See the Metrics-port verification for the harness shape.
3. Same-origin smoke: open `/#NAME` against a running server; confirm the
   `src-badge` reads `live` and the section renders on real data.
4. Toggle ink/paper — charts must re-theme (proves `retheme` is wired).

## Anti-patterns (redesign-specific)

| Anti-pattern | Why it's wrong |
|---|---|
| `type: "time"` Chart.js axis | No date adapter loaded — silently blank axis. Use category labels. |
| Hardcoded hex / `MetricColors` | Classic-era; breaks the paper theme. Read tokens via `getComputedStyle`. |
| `new Chart()` on every refresh tick | Flicker + leaks. Update datasets in place; rebuild only on theme change. |
| Full `innerHTML` rebuild of a section with a live `<select>` | Clobbers operator's selection. First-render once, update in place. |
| Calling `fetch` in a view | Bypasses the live-or-snapshot seam; the section stops rendering offline. |
| Looking for an allowlist / restarting the server | Neither exists for the redesign — files are served directly from `redesign/`. |

## When NOT to use this skill

- The standalone `phase.html` / `/phase` D3 view — different page, different rules.
- Pure server-side changes that don't render anything.
- Work in `agents/*/` resident agents — they don't have panels.
