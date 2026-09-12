---
name: unitares-dashboard
description: >
  Use when adding, editing, or reviewing sections on the unitares dashboard
  (dashboard/redesign/). Captures the redesign's conventions: the section-module
  pattern (window.X = { load }), the live-or-snapshot data seam, theme-aware
  charts via design tokens, and the app.html wiring (nav / pane / lazyLoad /
  RELOAD / retheme). A repo-specific reference — not general dashboard advice.
last_verified: "2026-09-12"
freshness_days: 30
source_files:
  - unitares/dashboard/redesign/app.html
  - unitares/dashboard/redesign/data.js
  - unitares/dashboard/redesign/ws.js
  - unitares/dashboard/redesign/snapshot.js
  - unitares/dashboard/redesign/tokens.css
  - unitares/dashboard/redesign/sections/eisv.js
  - unitares/dashboard/redesign/sections/metrics.js
  - unitares/dashboard/redesign/sections/landing.js
  - unitares/dashboard/redesign/sections/discoveries.js
  - unitares/dashboard/redesign/sections/telemetry-health.js
  - unitares/dashboard/redesign/sections/risk.js
  - unitares/dashboard/redesign/sections/adjudication.js
  - unitares/dashboard/redesign/sections/security.js
  - unitares/dashboard/package.json
  - unitares/dashboard/tests/telemetry-health.test.js
  - unitares/src/http_api.py
  - unitares/src/http_routes/dashboard.py
  - unitares/src/http_routes/sentinel.py
  - unitares/src/http_routes/telemetry.py
  - unitares/src/dashboard_auth.py
source_digests:
  unitares/dashboard/redesign/app.html: "ac37237f646e7bde"
  unitares/dashboard/redesign/data.js: "ee5ad0763d8ca6ec"
  unitares/dashboard/redesign/ws.js: "27bf5088dc4db8a3"
  unitares/dashboard/redesign/snapshot.js: "bfa64360fdcd4a90"
  unitares/dashboard/redesign/tokens.css: "ffad3d92c1033924"
  unitares/dashboard/redesign/sections/eisv.js: "9c05bfc8a7e8a7d5"
  unitares/dashboard/redesign/sections/metrics.js: "9412b1a03348fbea"
  unitares/dashboard/redesign/sections/landing.js: "814fce6a97c1139a"
  unitares/dashboard/redesign/sections/discoveries.js: "987622e5448645e7"
  unitares/dashboard/redesign/sections/telemetry-health.js: "b6fed2a82727ddcb"
  unitares/dashboard/redesign/sections/risk.js: "d36e47793c9b31e4"
  unitares/dashboard/redesign/sections/adjudication.js: "9b3ac40599eecc8d"
  unitares/dashboard/redesign/sections/security.js: "a9ebbfd8c2186a21"
  unitares/dashboard/package.json: "54b7e42849db8ab8"
  unitares/dashboard/tests/telemetry-health.test.js: "efbb8e0db3f89f6b"
  unitares/src/http_api.py: "1c6b7f1e3d840fce"
  unitares/src/http_routes/dashboard.py: "0fb5b075664dc0e6"
  unitares/src/http_routes/sentinel.py: "12566971b5a4c28b"
  unitares/src/http_routes/telemetry.py: "a49c4c1b1c5fcaed"
  unitares/src/dashboard_auth.py: "f2b1bbd42912995a"
---

# Adding a Section to the UNITARES Dashboard (redesign)

## Orientation

The live dashboard is **`dashboard/redesign/`** — buildless (raw HTML/CSS/JS,
no framework, no bundle). `http_dashboard_redesign` now lives in
`src/http_routes/dashboard.py`; `src/http_api.py` is the registration/re-export
facade that maps it to `/`, `/dashboard`, and `/dashboard/redesign/**`. The
classic dashboard and its allowlist / script-load-chain / `vite` build were
**retired** — ignore older guidance about `index.html`, `allowed_files`,
`MetricColors`, or `Chart.defaults`. The redesign resolver constrains paths and
file types and has no per-asset allowlist, but it does gate the assets that
carry governance data rather than presentation: `_AUTHENTICATED_ONLY_FILES`
holds `snapshot.js`, `preview.html` and `PLAN.md`, each served only to an
authenticated caller. The test is the data class, not the extension — the
latter two carry a real fleet capture and a description of the operator's own
fleet, and were public until 2026-09-12 because the gate was a filename set
and they landed beside the file it named. Only `snapshot.js` is loaded at
runtime; the other two are reference artifacts. `auth/*.html` is 404 on this
route (those pages are served via `/auth/*`). Files are read per request, so a restart is
not needed for static edits. Entry HTML is `no-store`; relative assets receive
an mtime version query to prevent stale browser bundles.

A "section" is one nav tab. Each is a self-contained module that renders into
its own mount and is wired in `app.html`.

## The section-module pattern

A section is an IIFE that attaches `window.<Name> = { load }` (add `retheme`
if it draws charts; add `applyEvent` / `notifyNew` if it's a live surface — see
**Live-surface hooks** below). `load()` renders into its mount on first call and
updates **in place** on subsequent calls (so a refresh doesn't flicker or reset
form state). Worked references: `sections/eisv.js` (charts, retheme,
`applyEvent`) and `sections/metrics.js` (charts + a picker that preserves its
selection across `load()` — exercised by its manual ↻, since Metrics is not on
the auto-refresh tick). `sections/telemetry-health.js` and `sections/risk.js`
are the newest sections and follow the same conventions.

One exception to know about: `sections/landing.js` (the Overview) does not
export `load`. It exports `{ render, refresh, refreshStats, applyEvent,
tickSilence }`, is booted by `window.Landing.render()` at the end of
`app.html`, and `RELOAD.overview` calls its `refresh` method. Do not copy it
as the template for a new section.

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
| 8 | If a refetch can show new data, add to `RELOAD` (polling fallback + WS doorbell; see **Auto-refresh discipline**) | `app.html` |
| 9 | If it draws charts, call `window.NAME.retheme()` in the theme-toggle handler | `app.html` |
| 10 | (live surface) Expose `applyEvent(msg)` → truthy when handled in place; register `APPLY.NAME = (msg) => window.NAME?.applyEvent?.(msg)` | `sections/NAME.js`, `app.html` |
| 11 | (badge) Expose `notifyNew()`; register `NOTIFY.<event_type> = () => window.NAME?.notifyNew?.()` | `sections/NAME.js`, `app.html` |

## Data seam — live-or-snapshot (Item 2)

Views never call `fetch` directly. They `await DATA.x()`, which returns
`{ source: "live" | "snapshot", data }`. The accessor tries the live endpoint
(`authFetch` for REST, `callTool` for `/v1/tools/call`) and falls back to the
bundled `SNAPSHOT` on any failure, so the dashboard renders portably (opened as
a file, cross-origin, or server down). `authFetch` carries same-origin passkey
session cookies and the optional bearer token. The `/ws/eisv` WebSocket
connects cookie-first (a browser cannot set headers on a socket); only if that
handshake fails early and a bearer is available does `ws.js` retry once with
`?token=` (`DATA.apiToken()`), and every later reconnect starts cookie-first
again. Badge freshness in the view with
`<span class="src-badge ${source}">${source}</span>`.

```js
async metricsCatalog() {
  return withFallback(
    async () => { const j = await authFetch("/v1/metrics/catalog");
                  return j && Array.isArray(j.metrics) ? j.metrics : null; },
    () => (S().metrics || {}).catalog ?? null,   // snapshot fallback
  );
}
```

Returning `null` from `liveFn` triggers the snapshot fallback. Accessors must
decide whether an empty array/object is valid live data and map it to `null`
themselves when it is not. For headline cards where a stale snapshot under a
"live" badge would mislead, prefer returning `null` per-field and rendering "—"
(see `data.js::stats`).

**The snapshot can be missing.** Since 2026-08 `snapshot.js` is auth-gated
when served, and `app.html` loads it with a plain `<script src>` that sends
cookies but not the bearer, so a bearer-authenticated operator gets 200 on every
REST call and 401 on `snapshot.js`. `window.SNAPSHOT` is then undefined: the
`S` accessor returns `{}`, and a `snapFn` that dereferences a missing key throws *out of*
`withFallback` and can take the whole view down (observed 2026-08-28 on the
Overview headline cards). Write the fallback defensively —
`() => (S().x || {}).y ?? null` — as `stats` does. Separately, a same-origin
401 with no bearer redirects to `/auth/signin` rather than falling back.

## Theme-aware charts (Item 9) — the real chart trap here

Chart.js still fights the theme, but the redesign fix is **design tokens**, not
hardcoded hex. Read colours from CSS custom properties (`tokens.css`) so the
chart re-renders correctly in both `ink` (dark) and `paper` (light):

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

`RELOAD[id]` runs while the section is active, the tab is visible, and no
input/select/textarea is focused. It is driven two ways: a ~10s polling tick
that fires **only while the `/ws/eisv` stream is not open**, and, with the
stream open, the debounced (~1.5s) WS doorbell in `onWsEvent`. It also runs
immediately on tab re-focus. Add a section to `RELOAD` only if a refetch can
show new data: daily-scrape or expensive-aggregate views (Metrics, Risk,
Telemetry, Automations) stay off it, load on nav, and offer a manual ↻ — the
comments on the `RELOAD` map in `app.html` record the measured cost that
decided each one. A live surface must be in `RELOAD` for its `applyEvent` to
run at all, because `onWsEvent` returns early for non-auto sections.

So `load()` must: update charts/data **in place** (don't `new Chart()` every
tick), and not rebuild a `<select>`/`<input>` the operator is using. The
refresh path already skips while an input is focused, so repopulating a closed
dropdown on refresh is safe — but preserve the current selection.
`sections/metrics.js` shows the first-render-then-update pattern.

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

## Mostly read-only — explicit authenticated write surfaces

The redesign sends the read bearer token everywhere; most sections are
read-only. Two areas intentionally mutate state:

- **Adjudication**: `DATA.adjudicate()` POSTs a Sentinel verdict
  (`confirmed`, `dismissed` with a reason, or `abstain`) to
  `/v1/sentinel/adjudicate` with `X-Unitares-Csrf: 1`; it may use a passkey
  dashboard session or the `X-Unitares-Operator` credential.
- **Security**: live-only accessors inspect/logout/revoke dashboard sessions,
  revoke passkeys, and mint enrollment codes. Session operations require the
  passkey session plus CSRF. Revoking a passkey needs the same, and revoking
  the **last** active passkey additionally needs a fresh (step-up) passkey
  sign-in or the `X-Unitares-Operator` credential. Minting an enrollment code
  needs the operator credential only (`POST /auth/enroll`, 403 otherwise).

The operator credential can be provisioned once via `?operator_token=…`
(persisted to localStorage and scrubbed from the URL by
`DATA.operatorToken()`). These live-only security calls deliberately do not fall
back to snapshots. Other operator actions such as agent archive/resume, review
requests, and discovery status changes are still not wired. Treat every new
write surface as a capability and design its auth/CSRF/failure behavior
explicitly.

## Verify before claiming done

The dashboard is buildless, so the gate is lint plus a cheap logic check:

1. `cd dashboard && npm run lint` — must be 0 errors (warnings allowed).
2. Headless logic drive: add a vitest case under `dashboard/tests/` and run
   `cd dashboard && npm test`. Harness shape (see
   `tests/telemetry-health.test.js`): `new JSDOM('<div id="NAME-mount"></div>',
   { runScripts: "outside-only" })`, stub `dom.window.DATA` and
   `dom.window.Chart`, `dom.window.eval(sectionSource)`, then
   `await dom.window.NAME.load()`; assert the mount populated and the chart
   constructor ran, and assert `app.html` contains `window.NAME.retheme()`.
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
| A `snapFn` that dereferences `S().x.y` | Throws out of `withFallback` when `snapshot.js` was not served. Guard every level. |
| Looking for an allowlist / restarting the server | No per-asset allowlist and no restart for the redesign (files are read per request). The only gates are the auth check on `snapshot.js` and the 404 for `auth/*.html`; the surviving `allowed_files` list serves only `/dashboard/phase.js`. |

## When NOT to use this skill

- The standalone `phase.html` / `/phase` D3 view — different page, different rules.
- Pure server-side changes that don't render anything.
- Work in `agents/*/` resident agents — they don't have panels.
