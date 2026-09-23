# Wave 1 §129 re-evaluation — T+0 = 2026-05-19

Sibling to `wave-1-window-evaluation-2026-05-18.md`. That doc evaluated the
T+0=2026-05-05 → T+13=2026-05-18 window and concluded condition 1 *cannot
be honestly evaluated* because the decorator emit site lacked
`payload.incident_id`. This doc anchors the next attempt under the
falsifier the prior doc named.

## Anchor

- **T+0 = 2026-05-19** — `fix(decorators): wire incident_id in mcp_handler_timeout emit` (PR #463, commit `ed34a769`, merged 2026-05-19). This is the one-site fix that lets the §129 metric
  `count(DISTINCT payload->>'incident_id')` measure incidents instead of NULLs.
- **T+14 = 2026-06-02** — earliest defensible evaluation date.

## Falsifier (lifted verbatim from the 2026-05-18 doc)

Condition 1 is substantively met iff all three hold:

1. `incident_id` is wired in the decorator emit payload
   (`src/mcp_handlers/decorators.py`). ✅ Shipped 2026-05-19 (#463).
2. A subsequent 14-day window runs under load comparable to T+0→T+6 of
   the prior window — `agent_state` writes ≥ 500/day averaged across the
   window.
3. `count(DISTINCT payload->>'incident_id')` over the window is zero.

The symmetric falsifier (amend the contract to use `count(*)` with
temporal-clustering dedup) is preserved as the alternative path but is
not taken here — the one-site fix already lands the original contract.

## Mechanics

`scripts/dev/section_129_reeval.py` runs the three conditions against the
governance DB. Default window is 2026-05-19 → 2026-06-02; flags
(`--start`, `--days`, `--json`) allow re-running over alternative windows.

Exit codes:

- 0 — all three conditions met (substantive pass)
- 1 — at least one condition not met
- 2 — window incomplete; informational run only

## Out of scope

- Conditions 2 and 3 of Wave 1 close (alarm parity, supervision fault
  absorption) are tracked elsewhere; this doc remains silent on them.
- This is one of four Wave 1 exit conditions; substantively passing
  condition 1 is necessary but not sufficient for Wave 1 close.

## Substrate-question impact

Per the prior doc's "What this evaluation supports":

- If condition 3 fails at the new window — i.e., counted incidents > 0 —
  the substrate-question evidence becomes concrete. That is the
  outcome that the Wave 0 instrumentation channel was set up to capture
  (per `beam-footprint-roadmap-v0.md` AMENDMENT 2026-05-04).
- If condition 3 passes — counted incidents == 0 across 14 days of
  representative load with the dedup field present — that's the
  first honest "no Wave 0 incidents" finding the project has produced.
  It does not close the substrate question on its own (Caveat 2 of the
  prior doc — five of six sub-types have zero production fires ever —
  still applies) but it is a real measurement, not a NULL artifact.

## What this doc deliberately does not do

- Does not redraft Wave 3 RFC. Per
  `feedback_redraft-cycle-bias-trap.md`, the 2026-05-09 v0.3.2 pause is
  measure-first; this doc IS the measurement track, not a fresh
  redraft.
- Does not re-litigate the destination. v0.3 RESOLUTION's A′ commitment
  binds; only the timing of the next Wave 3 re-attempt moves with the
  measurements.
