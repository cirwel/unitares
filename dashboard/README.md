# Unitares Governance Dashboard

**Created:** December 30, 2025
**Last updated:** May 2026
**Status:** Active static dashboard. Phoenix/LiveView migration is deferred, not currently the implementation path.

## Overview

The dashboard is the operator web UI for Unitares. It serves from the Python governance server and shows fleet health, agent state, EISV history, knowledge graph activity, dialectic sessions, resident status, and resident-specific panels for Watcher, Sentinel, Vigil, and Chronicler.

In production it is still served buildless: plain HTML, CSS, and JavaScript served directly from `dashboard/` by the Python server. A Vite + vitest + ESLint/Prettier toolchain now sits alongside that for local development and CI (see [Tooling and tests](#tooling-and-tests)); switching production serving to the bundled output is a deliberate, still-pending step documented there.

## Access

Start the governance server, then open:

- `http://127.0.0.1:8767/dashboard`
- `http://127.0.0.1:8767/` (same dashboard)
- `http://127.0.0.1:8767/phase` (phase-space view)
- `https://<your-domain>/dashboard` if the server is exposed through a tunnel

If `UNITARES_HTTP_API_TOKEN` is configured, provide the token either as:

- `?token=<token>` in the dashboard URL
- `localStorage.unitares_api_token`

The `authFetch` helper and `DashboardAPI` attach the bearer token for dashboard REST and tool calls.

### Operator token (write actions under STRICT_IDENTITY_REQUIRED)

The operator write buttons (archive/resume, config-set, dialectic-request) additionally need an operator credential once `STRICT_IDENTITY_REQUIRED` is on (#425): the server resolves a valid `X-Unitares-Operator` token to a stable operator identity, and that resolved binding is what passes the strict gate — reads keep working without it. Provide the token either as:

- `?operator_token=<token>` in the dashboard URL
- `localStorage.unitares_operator_token`

The token must be present in the server's `UNITARES_OPERATOR_TOKENS` allowlist (CSV env var; see `src/mcp_handlers/identity/operator.py` for storage and rotation guidance). With the flag off, the header is optional and changes nothing.

## Current Surfaces

- **Stats:** fleet coherence, active/total agents, stuck agents, discoveries, dialectic sessions, system health, calibration, anomalies, and trust-tier distribution.
- **Pulse:** latest governance decision, risk/confidence/complexity vitals, event sparkline, and pinned-agent support.
- **EISV:** fleet and per-agent time-series charts backed by Chart.js.
- **Agents:** searchable/filterable agent table with pagination, status, metrics, trust tiers, lineage/supersession badges, lifecycle reason display, and operator actions.
- **Discoveries:** recent knowledge graph entries with filters and status actions.
- **Dialectic:** peer-review/recovery sessions, phase/status counts, and transcript views.
- **Activity:** timeline of check-ins, verdicts, discoveries, dialectic events, lifecycle events, and resident events.
- **Residents:** always-on fleet strip with silence detection and recent writes.
- **Chronicler:** fleet metrics panel.
- **Watcher:** findings pipeline and pattern status panel.
- **Sentinel:** findings stream and severity/class breakdown.
- **Vigil:** janitor resident cycles and write stream.
- **Phase Space:** separate `/phase` view with E/I particles, basin contours, flow field, and live updates.

## Architecture

- **Frontend:** static `index.html`, `styles.css`, and JS modules in `dashboard/`.
- **Charts:** Chart.js for dashboard charts; the `/phase` page uses D3.
- **Tool calls:** `DashboardAPI.callTool()` posts to `/v1/tools/call`.
- **REST calls:** direct dashboard endpoints use the `authFetch` helper.
- **Live updates:** `/ws/eisv` streams EISV and broadcaster events. The UI falls back to polling where needed.
- **Refresh cadence:** full dashboard refresh every 30 seconds; API client cache defaults to 25 seconds.
- **Static-file guard:** `tests/test_dashboard_static_allowlist.py` ensures every `/dashboard/*.js` reference in `index.html` is allowlisted by `src/http_api.py`.

## Important Files

- `index.html` - main dashboard shell and section layout.
- `dashboard.js` - application orchestration, refresh loop, stats, modals, operator actions.
- `utils.js` - API client, authenticated fetch, cache/retry logic, formatting helpers, WebSocket client.
- `state.js` - shared dashboard state container.
- `agents.js` - agent table, filters, lineage display, live agent updates.
- `discoveries.js` - knowledge graph discovery panel.
- `dialectic.js` - dialectic session panel and transcript rendering.
- `eisv-charts.js` - EISV charts and WebSocket integration.
- `timeline.js` - activity timeline and event classification.
- `residents.js` - resident fleet strip.
- `fleet-metrics.js` - Chronicler/fleet metrics panel.
- `watcher.js` - Watcher findings panel.
- `sentinel.js` - Sentinel findings panel.
- `vigil.js` - Vigil panel.
- `phase.html` / `phase.js` - phase-space visualization.

## Backend Endpoints Used

Tool calls through `/v1/tools/call` include unified tools plus a few legacy dashboard/operator entry points:

- `agent(action="list" | "resume")`
- `knowledge(action="stats")`
- `search_knowledge_graph`
- `dialectic(action="list")`
- `archive_agent`
- `operator_resume_agent`
- `request_dialectic_review`
- `update_discovery_status_graph`
- `compare_agents`
- `detect_stuck_agents`
- `detect_anomalies`
- `check_calibration`
- `config(action="get" | "set")`

Dedicated HTTP endpoints include:

- `/health`
- `/api/events`
- `/api/activity`
- `/api/incidents`
- `/v1/residents`
- `/v1/residents/tag_audit`
- `/v1/watcher/summary`
- `/v1/sentinel/summary`
- `/v1/vigil/summary`
- `/ws/eisv`

Agent rows come from `agent(action="list", include_metrics=true, status_filter="all")`. The dashboard consumes compact list fields only: `parent_agent_id`, `spawn_reason`, `last_lifecycle_event`, `last_lifecycle_reason`, `last_lifecycle_at`, `superseded`, and `superseded_reason`. Full `identity_view` belongs to `get_agent_metadata` detail responses and is not required for the agent grid.

## Development

1. Start the server:

   ```bash
   python src/mcp_server.py --port 8767
   ```

2. Open `http://127.0.0.1:8767/dashboard`.
3. Edit files in `dashboard/` and refresh the browser.
4. If you add a JS or CSS file referenced by `index.html`, update the static allowlist in `src/http_api.py` and run the allowlist test.

Useful checks:

```bash
pytest tests/test_dashboard_static_allowlist.py
pytest tests/test_dashboard.py
```

## Tooling and tests

The dashboard has a Node toolchain for developer experience and CI. It is
**additive**: production still serves the raw files in `dashboard/`, so none of
this is required to run the dashboard — it makes the code pleasant to work on and
guards the render logic.

```bash
cd dashboard
npm ci              # install (vite, vitest, eslint, prettier, vendored chart.js)

npm run dev         # Vite dev server with HMR (see migration note below)
npm run build       # bundle src/main.js → dist/ (vendors Chart.js off the CDN)
npm test            # vitest: jsdom tests of the agent-list render/partition logic
npm run lint        # eslint (flat config; errors fail CI, legacy warnings allowed)
npm run format      # prettier --write on the maintained surface (src/tests/configs)
npm run format:check
```

CI runs `lint`, `format:check`, `test`, and `build` in the `dashboard` job
(`.github/workflows/tests.yml`).

### How the build is wired

`dashboard/src/main.js` is a single ES-module entry that vendors Chart.js and then
side-effect-imports the existing browser modules **in the same order
`index.html` declares them**. The modules are still global-attaching IIFEs; this
entry is the seam that lets the ESM migration happen incrementally instead of
big-bang. `npm run build` proves the whole chain bundles (Chart.js + all modules)
into `dist/assets/main-<hash>.js`.

### Migration note (production serving — not done yet)

Production still serves the raw `index.html` + `<script>` chain via
`http_dashboard` / `http_dashboard_static` in `src/http_api.py`, gated by the
static-file allowlist. Switching to the bundled output is intentionally a
separate step because it is coupled to two decisions:

1. **How the Pi serves the build** — commit `dist/`, build in CI and publish, or
   build on deploy. (`dist/` is gitignored today.)
2. **The allowlist** — fingerprinted `dist/assets/*` filenames can't be a fixed
   allowlist, so `http_dashboard_static` needs a directory-scoped, traversal-safe
   resolver, and `tests/test_dashboard_static_allowlist.py` updated to match.

Until that lands, `npm run dev` and `npm run build` are dev/CI aids; the served
dashboard is unchanged. A real-browser smoke test should gate the production
switch (the bundle is verified to compile and to initialize in jsdom, but not yet
rendered end-to-end in a browser from CI).

## Agent Visibility Checks

If an agent checked in but does not appear in the browser:

1. Clear the agent search box.
2. Set status to `All`.
3. Disable metrics-only and production-only filters.
4. Clear trust-tier filters by reloading the page.
5. Search by UUID prefix or exact label.
6. Hard refresh the browser if the API result is correct but the view is stale.

The dashboard API call is the browser list source of truth:

```bash
curl -s -X POST http://127.0.0.1:8767/v1/tools/call \
  -H "Authorization: Bearer $UNITARES_HTTP_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"name":"agent","arguments":{"action":"list","include_metrics":true,"recent_days":30,"limit":200,"min_updates":0,"status_filter":"all"}}'
```

If the agent appears in that response, ingestion and persistence are working. The remaining issue is usually a client-side filter, pagination state, cache, WebSocket lag, or stale browser state.

## Phoenix / LiveView Status

The repo has active Elixir/OTP work under `elixir/lease_plane/`, but that is the BEAM coordination kernel/lease plane, not a Phoenix dashboard rewrite.

`` explicitly lists these as deferred follow-up scope:

- Phoenix LiveView migration of the existing dashboard.
- Phoenix PubSub migration of the existing broadcaster, Discord bridge, and dashboard WebSocket plumbing.

So the dashboard README should stay current for the static dashboard. A Phoenix migration may still be a good direction later, especially for LiveView + PubSub, but there is no checked-in Phoenix app and no active dashboard migration branch in this repo.
