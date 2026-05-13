# Surface Taxonomy

**Status:** v0.1 draft. Editorial document, not architecture. Edit freely.

**v0.1 changes** (from council review of v0): dropped Per-agent proprioception category; replaced me-shaped/agent-shaped axis with read/write axis; added Action/Control and Trajectory-degradation categories; added age-since-onset note to the classifying question; flagged "operator" is not monolithic; corrected verdicts on Trust Tiers (delete, not relocate), Dialectic split (feature build, not relocate), Pulse (medium rewrite), stat-card relocation (medium-effort, not mechanical); rewrote apply-order around forking a thin new surface rather than surgically relocating signals on the existing page.

## Why

UNITARES has accumulated surfaces — the dashboard, MCP tools, the KG, Discord bridge, residents, alerts, the CLI — and most signals ended up on the dashboard by default rather than because the dashboard was the right home for them. The dashboard is confusing partly because every signal lacks a rule that sends it elsewhere.

This document is the rule.

It is editorial, not engineering. Categorization is necessary but not sufficient: the existing dashboard is too entangled (stat cards populated inline inside `loadAgents()`; Pulse on the shared `/ws/eisv` hot path with EISV charts, drift gauges, sparklines, and agent-card flash) for "relocate, don't delete" to be cheap. Plan around forking a thin new surface, not surgical reorganization.

## Reader

The dashboard's primary reader is **Kenny** (operator). Agents read UNITARES through MCP tools; outsiders read UNITARES through the paper and pitch deck.

"Operator" is not monolithic, though. **Glancing-Kenny** wants three glyphs and silence the rest of the time. **Debugging-Kenny** wants Pulse-panel depth, drift gauges, transcript views. Same human, different mode. The taxonomy below optimizes for glancing-Kenny on the front; debugging-Kenny gets a deliberate surface (existing dashboard, retained) and click-through paths.

## The Classifying Question

For any signal currently on a UNITARES surface:

> *If I saw this 30 seconds late, would anything be different?*

- **Yes** → fire-shaped (live, interruptive).
- **No** → deliberate-shaped (slow, dense, visited on purpose).

**Caveat (council finding):** the 30-second test is a *latency probe*, not a complete ontology. Many signals are fire-on-arrival and trend-after — stuck-agent at minute 1 is fire; at hour 6 is a backlog metric. Add an implicit *age-since-onset* axis: fresh fire interrupts; aging fire becomes a backlog signal that wants a different home. If proper alerting existed (Discord pings on resident silence, Sentinel anomaly, etc.), most fire signals collapse into "things that page" + their aging-trend tail. Treat the HUD as the surface for *fresh* fire only.

Second axis (replaces v0's me-shaped/agent-shaped, which carried no information once readers were stated):

> *Does seeing this signal terminate in looking, or in an action I take?*

- **Read** → dashboard / HUD / KG / Discord (information).
- **Write** → CLI tool, agents-table action button, MCP call (control).

This surfaces the **Action/Control** category — agents-table operator buttons (resume, archive, request-dialectic) are write surfaces, not read surfaces.

## Categories

| Category | Lives in | Why |
|---|---|---|
| **Fire** (fresh) | HUD surface (forked, new) | Live, glanceable, interrupts |
| **Trajectory degradation** | HUD surface or Discord ping | Specific class of fire: agents flat-lining, e.g. `progress_flat_candidate` events |
| **Aging-fire / backlog** | Existing dashboard analyst surface | What stuck-for-6-hours becomes |
| **Trends** | Existing dashboard analyst surface | Slow, dense, deliberate |
| **Discoveries / KG findings** | KG search + Discord notifications | Searchable + surfaced when relevant |
| **Coordination / who-is-where** | Discord bridge | Text-shaped, agent-native |
| **Action / Control** | Agents table + CLI tools + MCP | Write surfaces, not read surfaces |
| **Configuration / thresholds** | One config page (later) | Rare, deliberate |
| **Diagnostic / debug** | CLI tools, audit DB, Pulse-as-modal | Investigative; for debugging-Kenny mode |

(v0 had a "Per-agent proprioception" category. Removed: agents read MCP, operators don't need it visible, the Pulse surface that occupied it is structurally empty 96% of the time per runtime evidence.)

## Inventory: Current Dashboard Surfaces → Verdicts

Source: `dashboard/README.md` "Current Surfaces" + section scan of `index.html` + live runtime check.

| Surface | Category | Verdict | Implementation note |
|---|---|---|---|
| Residents strip (silence detection) | Fire | **Stays as HUD signal.** | Wire to forked `hud.html`. |
| Quick-status (qs-dot/label) | Fire | **Stays as HUD signal.** | Possibly merge with residents strip. |
| Fleet Coherence card | Trends | **Stays in analyst surface.** Don't surgically move. | Stat cards live in one DOM region populated by `loadAgents()` (`dashboard.js:953`); relocation is medium-effort. Let the existing dashboard *quietly become* the analyst surface. |
| Agents count card | Trends | Same — stays in analyst surface. | |
| Stuck Agents card | Fire (fresh) + Aging-fire (backlog) | **Emit count to HUD; full card stays in analyst surface.** | Aging matters: minute-1 stuck ≠ hour-6 stuck. |
| Discoveries count card | Trends | **Stays in analyst surface.** | Live data confirmed (801 total, 191 open); count alone not actionable. |
| Dialectic count card | Mixed | **Total stays in analyst surface.** "Needs you" filter does not exist in current data model — flagged as feature build, not relocation. | `loadDialecticSessions()` doesn't extract operator-pending status. Building it requires a backend `dialectic` tool change. |
| System Health card | Fire | **Emit to HUD.** | Reuse `/health` endpoint. |
| Calibration card | Trends | **Stays in analyst surface.** | |
| Anomalies card | Fire | **Emit count to HUD.** | Reuse `detect_anomalies` tool. |
| Trust Tiers card | Redundant | **Delete the standalone card.** | `agents.js` already renders trust_tier as a per-row badge from `agent(list, include_metrics=true)`. Standalone card is duplicate UI. |
| Pulse panel | Diagnostic (was: Per-agent proprioception) | **Reframe as click-through modal — but this is a medium rewrite, not a relocation.** | Wired as primary WS consumer at `eisv-charts.js:413, 526` for EISV + drift + sparkline + agent-card flash. Extracting it into a modal means either making the modal the WS listener target or threading visibility state through the WS handler. Do this *after* the forked HUD is up, not before. |
| EISV charts | Trends | **Stays in analyst surface.** | |
| Fleet Heatmap | Trends | **Stays in analyst surface.** | |
| Agents tab | Action/Control + Diagnostic | **Stays as analyst surface.** Operator action target. | Resume/archive/request-dialectic buttons are the write-surface for agent-level control. |
| Discoveries tab | Discoveries / KG | **Stays.** Primary discovery channel should be KG search + Discord notifications; tab is for backlog review. | |
| Dialectic tab | Diagnostic | **Stays as analyst surface.** Transcript view is investigative. | |
| Activity timeline | Trends | **Stays as analyst surface.** | |
| Chronicler panel | Trends | **Stays as analyst surface.** Includes external project-health metrics (github.cirwel.traffic.*, tests.unitares.count) — not previously named in taxonomy. | |
| Resident Progress (`resident-progress.js`) | Trends | **Stays in analyst surface.** | Missed in v0 inventory; renders inside Chronicler section as separate JS module. |
| Watcher panel | Fire (count) + Trends (stream) | **Split: HUD count + analyst stream.** This split is cheap. | `watcher.js:51` already isolates `by_status.surfaced + by_status.open` from `/v1/watcher/summary`. HUD only needs to fetch one field. |
| Sentinel panel | Fire (count) + Trends (stream) | **Split: HUD count + analyst stream.** Cheap. | `sentinel.js:35` derives counts from `by_severity` cleanly. |
| Vigil panel | Trends | **Stays as analyst surface.** Background cycles. | |
| Phase Space (`/phase`) | Trends | **Stays.** Already correctly separated. | |

## HUD Signal Set (target)

After folding council pushback, the fresh-fire HUD is approximately:

1. **Residents** — green / silent (one tile per resident)
2. **Stuck agents** — fresh count (aging tail is a backlog metric, not HUD)
3. **Vigil hi-severity** — fresh unresolved
4. **Sentinel anomalies** — active, fresh
5. **Watcher hi-severity unresolved count** — cheap to fetch
6. **Dialectic needing you** — *feature gated*: requires backend change to expose operator-pending status before this is wireable
7. **Lumen pulse** — alive / silent
8. **Lease plane health** — green / yellow / red
9. **Trajectory degradation** — `progress_flat_candidate` aggregate (specific class of fire, was unnamed in v0)

This list is editable. Anything that ages into backlog (stuck > 1h, anomaly unhandled > 1d) belongs on the analyst surface, not the HUD.

## What This Does Not Decide

- **Where the HUD lives.** Forked `hud.html` (recommended by council reviewer for cost reasons), Hermes-style TUI, Discord pinned, statusline, or menu bar — all are compatible with this taxonomy.
- **Phoenix LiveView vs polling.** Medium-agnostic. The lease plane is already Elixir/OTP — LiveView is plausible but not required. Polling existing endpoints from a static `hud.html` is the cheapest first move.
- **What the analyst surface looks like.** "Stays in analyst surface" only means the signal *belongs* there. The visual redesign of the existing dashboard is a separate, later question — and per the Honeycomb argument, the analyst surface may shrink over time as querying audit DB / KG directly becomes the primary investigation path.

## Open Questions

1. **Glancing-Kenny vs debugging-Kenny.** This taxonomy optimizes for glancing-Kenny on the HUD. Debugging-Kenny gets the existing dashboard plus click-through. Is that the right split, or should the HUD have a "more" toggle that expands into mid-detail without leaving the surface?
2. **Discoveries: tab or notification stream?** The KG already supports search; Discord already broadcasts. Tab is useful for backlog review but redundant for new findings.
3. **Configuration page.** Currently spread across thresholds modal, config tool, and per-resident settings. A single Hermes-web-style config page may be warranted. Out of scope for this taxonomy.
4. **Communal awareness for agents.** Agents currently coordinate via KG + `leave_note` + Discord. No web surface needed. If a coordination need emerges that the existing surfaces do not serve, that is a new problem — not a reason to add a dashboard tab.
5. **Aging-fire formalization.** The age-since-onset axis is named here but not operationalized. At what age does stuck-agent leave the HUD? Per signal, or one threshold? Probably per signal. To be specified when the HUD prototype gets concrete.

## Apply Order

Council reviewer's central correction to v0: **the dashboard does not shrink by surgical relocation; it shrinks by attrition.** The existing code is too entangled (stat cards in one DOM region, Pulse on a shared WS hot path, `dashboard.js:2327`'s `initStatCardNav()` hard-wires card-IDs to scroll targets) to relocate cheaply. Build the HUD as a fork, let the existing dashboard quietly become the analyst surface as the HUD takes over fresh-fire signals.

1. **Fork a thin `hud.html`** alongside the existing dashboard. Consumes existing endpoints — `/v1/watcher/summary`, `/v1/sentinel/summary`, `/v1/residents`, `/v1/eisv/latest`, `/health`, `/ws/eisv`. Add to `index.html`'s static allowlist (`tests/test_dashboard_static_allowlist.py` will catch a missed allowlist entry — known trip wire) and to `src/http_api.py`'s `allowed_files`.
2. **Wire the cheap splits first**: Watcher count, Sentinel count, residents strip. These are the verdicts council confirmed as low-cost.
3. **Delete the Trust Tiers standalone card.** Per-row badge in agents table covers it — the standalone card is duplicate UI.
4. **Defer the Pulse reframe** until the HUD is up and you can see whether you actually need it. Reframing it touches the WS hot path and is medium-rewrite — don't do it speculatively.
5. **Build "dialectic needs you" only if you actually want it on the HUD.** It requires a backend change to expose operator-pending status; not free.
6. **Leave the rest of the dashboard alone.** It becomes the analyst surface by *not adding to it*, not by deleting from it. Per the Honeycomb argument, it may shrink further over time as audit/KG queries replace some of its panels.

The HUD is small enough to prototype in a day. The taxonomy is the long-lived artifact; the HUD is a falsifiable test of the taxonomy.
