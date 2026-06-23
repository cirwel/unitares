# Proposal: Fleet-Severity Rollup into the Dashboard Hero

**Status:** **SHIPPED (Phase 1) — PR #875, live.** `dashboard/fleet-severity.js` (`computeFleetSeverity` pure fn) + 12 passing unit tests; `updateQuickStatus` consumes it; the four panels (system-health, watcher, sentinel, residents) publish their slice to `state` on both success and error paths; the `#needs-attention` band renders via `renderNeedsAttention`. Verified 2026-06-22: tests green, live server serves the module 200, allowlist + script tag present. Phase 2 (`/v1/fleet/status` server rollup) remains out of scope.
**Author:** Claude Code session (dashboard UX track)
**Date:** 2026-06-19 (shipped); verified 2026-06-22
**Scope:** `dashboard/` only (operator console). No server/API changes required for Phase 1.

## Problem

The dashboard fails the **5-second test** for fleet health. Two of the four
design-council reviewers (usability and IA/data-viz) independently flagged this
as the single most important gap between the current console and a commercial
monitoring product.

The top-of-page **Quick Status hero** (`#quick-status`, rendered by
`updateQuickStatus` in `dashboard.js`) is computed from **only two inputs**:

```js
// dashboard.js (refresh): the hero only ever sees agents + stuck agents
updateQuickStatus(cachedAgents, cachedStuckAgents);
```

Every other authoritative "something is wrong" signal lives in a panel **far
below the fold** and never rolls up:

| Signal | Where it lives today | Reaches the hero? |
|--------|----------------------|-------------------|
| Watcher critical findings | Watcher panel (`watcher.js`, bottom of page) | ❌ |
| Sentinel high/critical | Sentinel panel (`sentinel.js`) | ❌ |
| System Health `error` / `unavailable` (e.g. DB down) | System-Health card (`dashboard.js`, set independently) | ❌ |
| Resident silence (a resident stopped checking in) | Residents strip (`residents.js`, recomputed client-side) | ❌ |

**Concrete failure mode:** the Postgres/AGE database goes down. System Health
flips to `unavailable` in its own card, but the hero — fed only agent coherence
and stuck counts — still renders **"All systems healthy."** An operator glancing
at the top of the page is actively misled.

## Goal

An operator looking at the **top of the page for 5 seconds** can correctly answer
"is the fleet healthy, and if not, what needs attention?" — without scrolling.

## Proposal

Two additive pieces, no page reorder required:

### 1. Aggregate all severity sources into the hero

Change `updateQuickStatus` to take the worst severity across **all** known
signals, not just agents. The hero shows the **highest** severity present and
names the **driving reason**.

Proposed severity ladder (highest wins):

| Level | Hero state | Example driver |
|-------|-----------|----------------|
| `critical` | red | System Health unavailable; any agent critical; Watcher critical; Sentinel critical |
| `caution` | amber | Stuck agents; Sentinel high; a resident silent past its threshold |
| `healthy` | green | none of the above |

Hero text becomes severity + reason, e.g.
`⚠ Attention — DB unavailable` or `⚠ Attention — Watcher: 2 critical, 1 resident silent`,
instead of a flat "All systems healthy."

### 2. A compact "Needs attention" band under the hero

Directly beneath the hero, a single strip that renders **only when there are
active exceptions** — each a short chip with an anchor link to the relevant
panel:

```
⚠ Needs attention:  [DB unavailable →]  [Watcher: 2 critical →]  [3 stuck agents →]  [Vigil silent 45m →]
```

When everything is healthy the band is absent (no empty-state noise). This turns
the bottom-buried panels into a top-of-page exception feed **without reordering
the page** — purely additive markup + one render function.

## Severity inputs & where the data already is

All inputs are **already fetched** by existing panels; Phase 1 is about routing
their results to a shared aggregator, not new endpoints.

| Input | Source already in the client |
|-------|------------------------------|
| Agent critical / stuck | `cachedAgents` (`health_status === 'critical'`), `cachedStuckAgents` |
| System Health overall | `loadSystemHealth()` result / `#system-health-overall` |
| Watcher critical count | `watcher.js` summary fetch (`/v1/watcher/summary`) |
| Sentinel high/critical | `sentinel.js` summary fetch (`/v1/sentinel/summary`) |
| Resident silence | `residents.js` already recomputes "silent" client-side |

## Implementation sketch (Phase 1)

1. A small `computeFleetSeverity({...})` pure function (testable in the existing
   vitest/jsdom harness) that takes the counts above and returns
   `{ level, reasons: [{label, anchor, severity}] }`.
2. A shared store for the latest value from each panel (e.g. `state` keys
   `watcherSummary`, `sentinelSummary`, `systemHealthOverall`,
   `residentSilence`), each panel writing its slice on refresh.
3. `updateQuickStatus` consumes `computeFleetSeverity(...)` and the existing
   hero warning/critical CSS states (already wired in `styles.css`).
4. A `renderNeedsAttention()` that builds the band from the same `reasons`.

The pure function is the heart of it and is unit-testable, so the risky part
(the severity math) gets real coverage even though I can't render the page here.

## Risks & considerations

- **Timing / partial data:** panels refresh independently. The aggregator must
  treat "not yet loaded" as unknown (not healthy) and a *failed* fetch as a
  `caution`/`critical` signal in its own right (an unreachable Sentinel endpoint
  shouldn't read as "0 findings"). This dovetails with the separate
  error-vs-empty work.
- **No false alarms:** thresholds (what counts as "silent", which Sentinel
  severities escalate the hero) should be explicit and operator-tunable, not
  hardcoded magic numbers. Candidate for the Thresholds modal.
- **Don't regress the current hero:** keep the existing agent-coherence/stuck
  behavior as one input; this is additive.
- **Severity precedence is a judgment call** — see open questions.

## Open questions — resolved at ship (#875, operator-chosen 2026-06-19)

1. **Severity mapping:** critical → red, everything else → amber ("caution");
   *every* exception appears in the band regardless of level. Sentinel **high**
   and a single silent resident are amber (band chip + hero amber). See the
   policy header in `fleet-severity.js`.
2. **DB-down semantics:** System Health `unavailable` / `error` / `critical` →
   hero `critical`. Adopted.
3. **Resident silence:** per-resident, reusing `residents.js`'s existing
   client-side silence logic; it publishes `silentResidents` to `state`.
4. **Band placement:** directly under the hero (`#needs-attention` below
   `#quick-status`); hidden when the fleet is healthy.
5. **Scope:** Phase 1 client-side aggregation shipped. Phase 2 (`/v1/fleet/status`
   server rollup) remains out of scope.

## Effort

Phase 1 is **M** — one pure function (+ tests), four small panel "publish to
state" hooks, a hero-input change, and one render function + CSS for the band.
No server changes. The work is verifiable in CI (the severity function) except
the final visual placement, which needs an in-browser check.
