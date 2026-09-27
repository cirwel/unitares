---
name: unitares-dashboard
description: >
  Use when adding, editing, or reviewing sections on the unitares dashboard
  (dashboard/redesign/). Captures the redesign's conventions: the section-module
  pattern (window.X = { load }), the live-or-snapshot data seam, theme-aware
  charts via design tokens, the app.html wiring (nav / pane / lazyLoad /
  RELOAD / retheme), and the core-vs-extension boundary (deployment-specific
  tabs live outside the repo, served from UNITARES_DASHBOARD_EXT_DIR). A
  repo-specific reference — not general dashboard advice.
last_verified: "2026-09-26"
freshness_days: 30
source_files:
  - unitares/dashboard/redesign/app.html
  - unitares/dashboard/redesign/data.js
  - unitares/dashboard/redesign/ws.js
  - unitares/dashboard/redesign/snapshot.js
  - unitares/dashboard/redesign/tokens.css
  - unitares/dashboard/redesign/sections/eisv.js
  - unitares/dashboard/redesign/sections/landing.js
  - unitares/dashboard/redesign/sections/discoveries.js
  - unitares/dashboard/redesign/sections/risk.js
  - unitares/dashboard/redesign/sections/security.js
  - unitares/dashboard/package.json
  - unitares/dashboard/EXTENSIONS.md
  - unitares/dashboard/tests/risk-history.test.js
  - unitares/dashboard/tests/app-extensions.test.js
  - unitares/src/http_api.py
  - unitares/src/http_routes/dashboard.py
  - unitares/src/http_routes/packs.py
  - unitares/src/http_routes/telemetry.py
  - unitares/src/governance_trend.py
  - unitares/dashboard/tests/landing-agent-first.test.js
  - unitares/src/dashboard_auth.py
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
holds `snapshot.js` and `PLAN.md`, each served only to an authenticated
caller. The test is the data class, not the extension. `snapshot.js` has been a
synthetic fixture since 2026-09-27 and stays gated anyway; `PLAN.md` describes
the operator's own fleet. `preview.html` (a literal fleet capture) was deleted
then. Only `snapshot.js` is loaded at runtime. Never bundle a real capture:
new offline data goes into the synthetic generator in `snapshot.js`. `auth/*.html` is 404 on this
route (those pages are served via `/auth/*`). Files are read per request, so a restart is
not needed for static edits. Entry HTML is `no-store`; relative assets receive
an mtime version query to prevent stale browser bundles.

A "section" is one nav tab. Each is a self-contained module that renders into
its own mount and is wired in `app.html`.

## Core or extension? Decide first

The shipped tabs are Overview, Agents, Discoveries, Dialectic, Activity, EISV,
Risk and Security. A new tab belongs in core only if **any install** can read
it: it renders on an empty roster, names no resident, and describes product
state rather than one deployment's instruments. Otherwise it is an
**extension**: it lives outside this repo, in the directory named by
`UNITARES_DASHBOARD_EXT_DIR`, and `app.html` loads it from that directory's
`manifest.json` at `/dashboard/ext/`. The contract is `dashboard/EXTENSIONS.md`.

The Residents, Automations, Adjudication, Telemetry, Metrics and Enforcement
tabs moved out on 2026-09-26 on exactly this test. Their server endpoints stay;
only the views left. The same test shaped the Overview (2026-09-27): it leads
with agents, not residents — cards Agents (checked in within the hour),
Check-ins (by verdict), Dialectic (failure rate first), Discoveries, System
Health; a "Latest check-in" panel and feed over every agent from
`/v1/eisv/agents`; and the resident strip only when the deployment configures
residents. Fleet Coherence left the row: its between-agent spread is ~0.008, so
it could not move. Risk's trend reads `/v1/governance/trend` (computed from
`core.agent_state`, capped at 60 days by retention), not a resident's metric scrape,
and its picker lists recent agents. Totals over the server's in-memory rings
(`/api/activity`, `/v1/eisv/agents`) carry a `coverage_start` and say when the
hour is only partly covered. Adding a card back is the same decision.

An extension follows the section pattern below unchanged. It skips the
checklist's `app.html` rows, because the manifest supplies nav, pane, mount,
`auto` and the script, and `app.html` calls `retheme()` on every registered
extension. Its accessors go in a manifest `scripts` file built on `DATA.seam`
(`authFetch`, `callTool`, `withFallback`), and its fallbacks are empty shapes,
never a bundled capture. The `/dashboard/ext/` route is 404 when the variable is
unset and 401 to an unauthenticated caller; `DATA.extManifest()` treats both as
"no extensions" and never redirects to sign-in.

## The section-module pattern

A section is an IIFE that attaches `window.<Name> = { load }` (add `retheme`
if it draws charts; add `applyEvent` / `notifyNew` if it's a live surface — see
**Live-surface hooks** below). `load()` renders into its mount on first call and
updates **in place** on subsequent calls (so a refresh doesn't flicker or reset
form state). Worked references: `sections/eisv.js` (charts, retheme,
`applyEvent`) and `sections/risk.js` (charts + a resident picker that preserves
its selection across `load()` — exercised by its manual ↻, since Risk is not on
the auto-refresh tick).

One exception to know about: `sections/landing.js` (the Overview) does not
export `load`. It exports `{ render, refresh, refreshStats, applyEvent,
tickSilence }`, is booted by `window.Landing.render()` at the end of
`app.html`, and `RELOAD.overview` calls its `refresh` method. Do not copy it
as the template for a new section.

## Integration checklist for a core section (all in `dashboard/redesign/`)

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
`{ source: "live" | "snapshot" | "unavailable", data }`. The accessor tries the live endpoint
(`authFetch` for REST, `callTool` for `/v1/tools/call`) and, on any failure,
returns its `snapFn` result tagged `snapshot`. The bundled `SNAPSHOT` backs
that fallback only when there is no server to ask — the page opened from a
file — or when a design preview passes `?snapshot=1` (`SNAPSHOT_FALLBACK` in
`data.js`). On a page served by a UNITARES server, the `S` accessor returns `{}`: the bundle is
example data (a real capture until 2026-09-27), and a failed read there means
this server blipped, so it must render "unavailable", never example or another
deployment's residents, EISV or version (a fresh install showed the bundled fleet as its own
after one failed read, 2026-09-26). `authFetch` carries same-origin passkey
session cookies and the optional bearer token. The `/ws/eisv` WebSocket
connects cookie-first (a browser cannot set headers on a socket); only if that
handshake fails early and a bearer is available does `ws.js` retry once with
`?token=` (`DATA.apiToken()`), and every later reconnect starts cookie-first
again. Badge freshness in the view with
`<span class="src-badge ${source}">${source}</span>`.

```js
async riskTrend(days) {
  return withFallback(
    async () => { const j = await authFetch("/v1/governance/trend?days=" + d);
                  // A successful empty series is live ("no risk readings yet"),
                  // not an outage: only a failed or malformed response is null.
                  if (!j || !j.success || !Array.isArray(j.risk)) return null;
                  return { windowDays: j.window_days, risk: j.risk, pause: j.pause, guide: j.guide }; },
    () => S().riskTrend || { windowDays: d, risk: [], pause: [], guide: [] },   // empty shape
  );
}
```

Returning `null` from `liveFn` triggers the fallback. Accessors must decide
whether an empty array/object is valid live data, and map it to `null` only
when it is not: a producer that ran and found nothing is live data, an outage
is not. For headline cards where a stale snapshot under a
"live" badge would mislead, prefer returning `null` per-field and rendering "—"
(see `data.js::stats`).

**Every `snapFn` must render as empty.** On a served page the `S` accessor
always returns `{}`, and `withFallback` tags the result `unavailable`, not
`snapshot`. A fallback that dereferences a nested key (`S().x.y`) throws *out of*
`withFallback` on the first failed read and the pane never renders. Return the
shape the view expects, empty — `() => (S().x || {}).y || []` —
as the dialectic and activity accessors in `data.js` do.
`dashboard/tests/sections-unavailable.test.js` loads every core section with
all requests failing and fails if any rejects or shows bundled data; add new
sections to its table. An extension's accessors have no bundled snapshot at
all, so their fallbacks are always the empty shape.

**The snapshot can be missing.** Since 2026-08 `snapshot.js` is auth-gated
when served, and `app.html` loads it with a plain `<script src>` that sends
cookies but not the bearer, so a bearer-authenticated operator gets 200 on every
REST call and 401 on `snapshot.js`. `window.SNAPSHOT` is then undefined: the
`S` accessor returns `{}`, and a `snapFn` that dereferences a missing key throws *out of*
`withFallback` and can take the whole view down (observed 2026-08-28 on the
Overview headline cards). Write the fallback defensively —
`() => (S().x || {}).y ?? null` — as `stats` does. Separately, a same-origin
401 with no bearer redirects to `/auth/signin` rather than falling back. On an
install with no `UNITARES_DASHBOARD_RP_ID`, passkey sign-in is off and that page
answers 503 naming the keys to set, so the redirect ends there.

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
`sections/risk.js::fmtDay`.

## Auto-refresh discipline (Item 8)

`RELOAD[id]` runs while the section is active, the tab is visible, and no
input/select/textarea is focused. It is driven two ways: a ~10s polling tick
that fires **only while the `/ws/eisv` stream is not open**, and, with the
stream open, the debounced (~1.5s) WS doorbell in `onWsEvent`. It also runs
immediately on tab re-focus. Add a section to `RELOAD` only if a refetch can
show new data: daily-scrape or expensive-aggregate views (Risk; among the
extensions, Metrics, Telemetry and Automations) stay off it, load on nav, and
offer a manual ↻ — the comment on the `RELOAD` map in `app.html` records why.
An extension joins `RELOAD` only when its manifest entry sets `auto`. A live surface must be in `RELOAD` for its `applyEvent` to
run at all, because `onWsEvent` returns early for non-auto sections.

So `load()` must: update charts/data **in place** (don't `new Chart()` every
tick), and not rebuild a `<select>`/`<input>` the operator is using. The
refresh path already skips while an input is focused, so repopulating a closed
dropdown on refresh is safe — but preserve the current selection.
`sections/risk.js` shows the first-render-then-update pattern.

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

The redesign sends the read bearer token everywhere; core sections are
read-only except one area. (A finding-adjudication write endpoint, with
`X-Unitares-Csrf: 1`, is served only when the `reference-residents` route pack
is mounted via `UNITARES_ROUTE_PACKS`, and its view is an extension. An
extension that calls a pack route needs that pack enabled on the server.)

- **Security**: live-only accessors inspect/logout/revoke dashboard sessions,
  revoke passkeys, and mint enrollment codes. Session operations require the
  passkey session plus CSRF. Revoking a passkey needs the same, and revoking
  the **last** active passkey additionally needs a fresh (step-up) passkey
  sign-in or the `X-Unitares-Operator` credential. Minting an enrollment code
  needs the operator credential only (`POST /auth/enroll`, 403 otherwise).
  All of these passkey ceremonies (sign-in, enrollment in both methods, and the
  four `/auth/webauthn/*` steps) first require a configured RP id: with
  `UNITARES_DASHBOARD_RP_ID` unset they answer 503 before any credential check.
  A passkey is bound to one domain, so there is no default. On such an install
  the session read also fails (403), so when it does the tab asks
  `DATA.passkeyConfig()` (a credential-free `GET /auth/enroll`) and shows the
  server's named fix as a neutral setup hint rather than a red session error.
  Only that exact 503 body counts as "not configured"; any other answer, or
  none, keeps the error.

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
   `tests/risk-history.test.js`): `new JSDOM('<div id="NAME-mount"></div>',
   { runScripts: "outside-only" })`, stub `dom.window.DATA` and
   `dom.window.Chart`, `dom.window.eval(sectionSource)`, then
   `await dom.window.NAME.load()`; assert the mount populated and the chart
   constructor ran, and assert `app.html` contains `window.NAME.retheme()`.
   An extension's specs live with the extension and evaluate core
   `redesign/data.js` before its own scripts; the loader's own contract is
   `tests/app-extensions.test.js`.
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
| A `snapFn` that dereferences `S().x.y` | Throws out of `withFallback` on every served page (the `S` accessor returns `{}` there). Guard every level; `tests/sections-unavailable.test.js` catches it. |
| Looking for an allowlist / restarting the server | No per-asset allowlist and no restart for the redesign (files are read per request). The only gates are the auth check on `snapshot.js` and the 404 for `auth/*.html`; the surviving `allowed_files` list serves only `/dashboard/phase.js`. |
| A deployment-specific tab in core | An outside operator inherits it. Resident names, one machine's job census, a research instrument: ship it as an extension. |

## When NOT to use this skill

- The standalone `phase.html` / `/phase` D3 view — different page, different rules.
- Pure server-side changes that don't render anything.
- Work in `agents/*/` resident agents — they don't have panels.
