# Wave 1 (Sentinel-on-BEAM) completion status — 2026-06-14

Status: read-only status roll-up across the Wave 1 implementation surfaces
and the four exit conditions. Consolidates the implementation evidence in
`elixir/sentinel/` with the operational-evaluation track
(`wave-1-window-evaluation-*.md`, `section-129-measurement-fix-2026-06-03.md`)
so the close decision rests on one ledger rather than scattered docs. Does
**not** declare Wave 1 closed — that remains an operator decision under the
condition-4 anti-enthusiasm guard.

## Bottom line

Wave 1 is **implemented, shipped, and running in production, but not formally
closed.** The codebase distinguishes "the port shipped" from "the exit
criteria passed," and this doc keeps that distinction explicit:

- **Implementation:** complete. All five state surfaces are wired; Surfaces
  1 + 2 shipped (`beam-footprint-roadmap-v0.md` notes "Wave 1 shipped
  Surface 1+2 successfully"); the BEAM Sentinel runs as
  `com.unitares.sentinel-beam` (`docs/ontology/plan.md`).
- **Exit conditions:** not all closed. Condition 1's measurement gate only
  became trustworthy on 2026-06-03 and still owes a representative-load
  window; conditions 2 and 3 have strong test coverage but no operational
  close declaration; condition 4 is the guard that holds the line.

## Implementation surfaces (per RFC `beam-wave-1-sentinel.md`)

The RFC enumerates five Sentinel-owned state surfaces. All are wired in
`elixir/sentinel/`:

| # | Surface | Module | Status |
|---|---------|--------|--------|
| 1 | `STATE_FILE` cycle state (`.sentinel_state` / `.sentinel_state.beam`) | `cycle_state.ex`, `atomic_write.ex`, `cutover.ex` | Shipped. Combined-poller topology (v0.1.3 §B1) in `forced_release_poller.ex`; atomic-write helper matches Python's chmod-0o600 / no-fsync contract. |
| 2 | Findings emit channel → `POST /api/findings` | `findings.ex`, `fleet_finding_emitter.ex`, `forced_release_poller.ex` | Shipped. Three alarm classes (ad_hoc, deprecation_batch, conflict_batch) ported with Python-equivalent fingerprint formulas. |
| 3 | Lease-advisory scope `resident:/sentinel_cycle` | `lease_advisory.ex` | Shipped (Phase A advisory). Fleet-emit uses a distinct surface `resident:/sentinel_fleet_emit` to avoid self-collision. |
| 4 | anyio mitigations | n/a (not inherited) | N/A — BEAM has no anyio loop; the workaround is structurally unnecessary. |
| 5 | `SESSION_FILE` identity continuity (`sentinel.json`) | `session_anchor.ex` | Shipped. Read + pre-cutover backup; schema stays forwards-compatible with Python's `GovernanceAgent._ensure_identity`. |

Supporting machinery shipped: `mix sentinel.cutover` / `sentinel.rollback` /
`sentinel.cursor_diff` / `sentinel.session_backup`, `eisv_web_socket.ex`
(WS `/ws/eisv` ingest), `fleet_state.ex` / `fleet_analysis.ex`
(Python `FleetState.analyze/1` rule port), and `governance_checkin.ex`.

## Exit conditions

The roadmap (`beam-footprint-roadmap-v0.md`) lists four:

### Condition 1 — zero coordination-class incidents over a 14-day window (§129)

**Status: gate trustworthy as of 2026-06-03; representative-load window still
owed.**

The §129 measurement gate was *doubly broken* and only fixed on 2026-06-03
(PR #576, `section-129-measurement-fix-2026-06-03.md`):

- **Bug 1 (nesting blindness):** `incident_id` is stored nested at
  `payload->'payload'->>'incident_id'` in `audit.events`, but §129 queried
  the flat path — blind to a field present on every row.
- **Bug 2 (shutdown noise):** graceful-shutdown task cancellations were
  emitted as `coordination_failure.anyio_cancellation.background_task` and
  would have been miscounted as substrate incidents.

The corrected gate reads **0 true substrate-tax incidents** for the Wave 1
window (the 69 in-window rows collapse to 8 server-restart fanout bursts).
But two caveats keep condition 1 open:

1. **Low coverage** — 5 of 6 wired `coordination_failure` sub-types have
   never fired in production; the one that historically fired
   (`mcp_handler_timeout.tool_decorator`) went silent after a perf fix. A
   zero means "no instrumented failure mode fired," not strong "stay Python"
   evidence.
2. **Unrepresentative load** — the measured window ran while the operator
   was AFK (~16× below the T+0→T+6 reference load). The residual follow-up
   in `section-129-measurement-fix-2026-06-03.md` still owes a **fresh
   forward window at ≥500 `core.agent_state` writes/day** now that the gate
   is trustworthy.

### Condition 2 — alarm-rule parity with the Python Sentinel

**Status: strong implementation-level coverage; no operational parity audit
declared closed.**

- `FleetAnalysis` (`fleet_analysis.ex`) ports Python's `FleetState.analyze/1`
  rules (coordinated degradation, entropy outliers, verdict shift) with the
  same thresholds.
- Forced-release alarms are covered per-class by `forced_release_poller_logic_test.exs`,
  `..._logic_3class_test.exs`, `..._integration_test.exs`,
  `..._3class_integration_test.exs`, and `..._findings_test.exs`.
- A Tier-2 cross-runtime state contract exists: `tests/test_sentinel_cross_runtime_state.py`
  pins that Python's `load_state` recovers the cursor from a BEAM-written
  `.sentinel_state.beam` fixture (and the symmetric direction in
  `cycle_state_test.exs`).

**Update 2026-06-14:** the documented parity audit now exists —
`wave-1-condition-2-alarm-parity-audit-2026-06-14.md`. Its verdict: the four
fleet-analysis rules and 2 of 3 forced-release alarm fingerprints were at
parity, with **two confirmed dedup gaps** — (1) the conflict_batch fingerprint
diverged across runtimes (`+00:00` vs `Z` ISO suffix; the §C3-flagged drift,
untested by the self-referential BEAM test), and (2) fleet-finding fingerprints
only deduped if `UNITARES_SENTINEL_AGENT_ID` was set to Python's anchor UUID.
Both would cause double-fire at the cutover gap, which condition 2 exists to
prevent. **Both are now fixed in this branch** (`logic.ex` `iso8601_python/1`;
`application.ex` threads the anchor UUID) with the §B2 cross-runtime contract
pinned by `tests/test_sentinel_forced_release_fingerprint_parity.py` +
the updated Elixir 3-class test. Python side validated locally; Elixir gated
on CI `mix test`. Remaining: the live cutover double-fire check.

### Condition 3 — supervision tree absorbs ≥1 induced fault, no manual intervention

**Status: mechanism proven by an automated induced-fault test; live
production observation still owed.**

- The OTP supervisor is `:one_for_one` (`application.ex`).
- `forced_release_poller_structure_test.exs:51` ("tick on dead DB exits —
  supervisor restart preserves cursor") pins the §B6 path (b): a dead-DB
  tick *exits* (so the supervisor restarts and `init/1` re-reads the
  on-disk cursor) and does **not** write a partial shadow file.
- **New 2026-06-14:** `supervision_restart_test.exs` closes the
  mechanism directly — a `:one_for_one` supervisor (mirroring
  `Application`'s strategy) restarts a **killed** `ForcedReleasePoller`
  child with no manual intervention, a fresh pid appears under the same
  name, and the restarted worker re-reads the on-disk cursor (de-dup fence
  survives the fault).

Gap to close: the roadmap's condition is operational — "kill a worker,
supervisor restarts, no manual intervention" *observed in the running
deployment*. The automated test now demonstrates the mechanism end-to-end;
the remaining item is a single induced-fault observation against the live
`com.unitares.sentinel-beam` (which the automated test cannot stand in for
under the condition-4 anti-enthusiasm guard).

### Condition 4 — anti-enthusiasm guard

**Status: active — this is the constraint, not a measurement.** The operator
must not declare success on enthusiasm; the 14-day window *and* the Wave 0
incident feed must both hold before Wave 1 closes. The evaluation docs
record "Wave 1 close: not recommended" and treat condition 1 as necessary
but not sufficient.

## Summary table

| Condition | Implemented | Operationally closed |
|-----------|-------------|----------------------|
| 1 — §129 zero incidents | gate fixed (2026-06-03) | **No** — owes representative-load window |
| 2 — alarm parity | yes (unit + cross-runtime state) | **No** — audit done + both dedup gaps fixed (2026-06-14, Elixir pending CI); live cutover double-fire check remains |
| 3 — supervision fault absorption | yes (topology + automated induced-fault test, 2026-06-14) | **No** — live induced-fault observation still owed |
| 4 — anti-enthusiasm guard | n/a (guard) | guard holds |

## What would close Wave 1

1. Run a fresh §129 window at ≥500 `core.agent_state` writes/day with the
   corrected gate; confirm `distinct_incidents = 0`.
2. Record an end-to-end alarm-parity comparison (condition 2) and a live
   induced-fault supervision-recovery observation (condition 3).
3. Operator close decision under condition 4, with both the 14-day window and
   the Wave 0 incident feed holding.

## Cross-references

- RFC: `docs/proposals/beam-wave-1-sentinel.md`
- Roadmap exit criteria: `docs/proposals/beam-footprint-roadmap-v0.md`
- §129 track: `docs/proposals/resolved/wave-1-window-evaluation-2026-05-18.md`,
  `wave-1-window-evaluation-T0-2026-05-19.md`,
  `section-129-measurement-fix-2026-06-03.md`
- Implementation: `elixir/sentinel/`
- State ledger: `docs/ontology/plan.md`
