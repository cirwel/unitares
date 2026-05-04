---
title: Sentinel ephemeral events vs KG — separating signal from memory
author: claude_code-claude (250db6d1)
date: 2026-04-25
status: SHIPPED — Sentinel double-write to KG removed via PR #154 (2026-04-25); high-severity findings now flow only through `/api/findings`
revisions:
  - 2026-04-25: initial draft
  - 2026-04-25: revised after council review (durability flaw, type-name mismatch, sizing)
  - 2026-04-25: shipped via PR #154
---

# Sentinel ephemeral events vs KG

## Problem

Sentinel writes every high-severity fleet finding to two places:

1. `/api/findings` via `post_finding()` — fingerprinted, dedup'd, broadcast to dashboard (`agents/sentinel/agent.py:524`)
2. KG via `leave_note()` — plain leave_note, no dedup, persists forever (`agents/sentinel/agent.py:548-557`, routed through `agents/sdk/src/unitares_sdk/agent.py:374-380`)

The KG entries are ephemeral fleet snapshots — "Pause rate 27% in last 10min", "entropy outlier (z=2.3)". Today's KG sweep archived four such notes from 2026-04-23 alone. They have zero archival value 24h later: the 10-minute window has passed, the agents involved may not even still be running, and the dashboard already captured the signal in real time via path #1.

## Three load-bearing facts (verified during council review)

### Fact 1: The Vigil-Sentinel KG coordination doesn't actually work today

Sentinel's emit types: `entropy_outlier`, `verdict_shift`, `correlated_events` (`agents/sentinel/agent.py:227, 249, 266`).

Vigil's audit-trigger set: `{"verdict_distribution_shift", "correlated_governance_events"}` (`agents/vigil/agent.py:215-218`).

The names don't match. `sentinel_force_audit` at `agents/vigil/agent.py:439-441` checks `f["type"] in _SENTINEL_AUDIT_TRIGGERS` — and never fires on real Sentinel output. The coordination is dead at the type-name level. Vigil reads the notes, references them in its summary line ("Sentinel/verdict_shift: ..."), but never escalates to a forced groundskeeper pass.

This means: removing Sentinel's KG writes today breaks nothing that is currently working. The "coordination feature we'd be regressing" is aspirational.

### Fact 2: `post_finding` does NOT persist to `audit.events`

The original draft assumed the findings stream was Postgres-backed via the broadcaster's fire-and-forget persistence. It isn't. `http_record_finding` (`src/http_api.py:1657, 1690-1696`) calls `event_detector.record_event()` which writes only to a 500-event in-memory ring buffer (`src/event_detector.py:381-418, 531`). On MCP server restart, the buffer is empty.

`broadcaster._persist_event` (`src/broadcaster.py:118-132`) is only called from `broadcast_event`, not from `record_event`. `GET /api/events` (`src/http_api.py:922, 946-986`) supplements from `audit.events` only for events that *were* persisted — which sentinel findings via `post_finding` never were.

A future Vigil-reads-from-stream design must address persistence; otherwise it trades a Postgres-backed durable store for a dashboard cache.

### Fact 3: `/api/findings` GET doesn't exist

`src/http_api.py:2345` registers POST only. The closest existing GET is `/api/events?type=sentinel_finding` (route at `src/http_api.py:2344`, handler at line 922). It accepts `event_type`, `agent_id`, integer `since` cursor, and `limit`. The integer cursor resets to 0 on restart, which would mismatch Vigil's ISO-timestamp `cycle_time` state.

## Phased plan

### Phase 1 (this PR) — stop the bleeding

**Sentinel:**
- Remove the `note_tuples` block at `agents/sentinel/agent.py:548-557`
- `CycleResult.notes` is no longer populated for fleet findings (it remains available for any future state-transition notes)
- `post_finding()` call at line 524 is unchanged — still emits to dashboard with fingerprint dedup

**Vigil:**
- No code changes. `_read_sentinel_findings` returns empty after this PR (Sentinel stopped writing) — behavior is as if no high-severity findings happened, which equals the production reality today (the audit-trigger never fired anyway, per Fact 1).

**Deferred to Phase 2/3:**
- SDK `events` channel — premature abstraction with one user. Add when Steward or Chronicler need it.
- Vigil read-path migration — needs persistence fix (Fact 2) first.

**What this fixes:**
- Sentinel KG noise stops. Future pause-rate / entropy-outlier entries don't accumulate.

**Tradeoff accepted (closed by Phase 3):**
- Vigil's check-in summary line `"Sentinel/foo: bar"` no longer appears. Operators reading Vigil check-ins lose attribution; the same data is still on the dashboard via `/api/findings` and via Sentinel's own check-ins. The line was already not triggering audit work due to the type-name mismatch.

**Test:** new regression `test_run_cycle_does_not_write_findings_to_kg` in `agents/sentinel/tests/test_findings_emit.py` asserts `result.notes is None` after a cycle with high-severity findings.

### Phase 2 (separate PR) — fix the coordination contract + persistence

Two coupled fixes:
- Align type names. Pick one (recommendation: the longer Sentinel-emit names `verdict_shift` / `correlated_events` are tighter; update Vigil's `_SENTINEL_AUDIT_TRIGGERS` set to match). Update `agents/vigil/tests/test_sentinel_coordination.py` mock data accordingly.
- Make `http_record_finding` call `await broadcaster_instance.broadcast_event(...)` after accepting a finding, so it triggers `_persist_event` to `audit.events`. Verify `audit.events` retention covers Vigil's 30-min cycle interval.

**What this fixes:** the Vigil-Sentinel coordination *actually* fires when warranted; findings stream becomes durable across restarts.

### Phase 3 (separate PR) — migrate Vigil's read path

- Replace `_read_sentinel_findings` with `_read_findings_stream` querying `GET /api/events?type=sentinel_finding&...` (or a new `GET /api/findings` if filter shape doesn't fit).
- Specify severity=high filter explicitly (load-bearing — Vigil's current path requires it via `_filter_sentinel_findings:235-236`).
- Convert ISO `cycle_time` cursor adapter for the integer `since` parameter, OR (preferred) change the endpoint to accept ISO timestamps.
- Once verified, drop Vigil's KG-coordination dead code (the leftover from Phase 1).

This is the work the council originally sized at 250-500 LoC. Doing it as Phase 3 means it lands on a foundation that already works (Phase 2's persistence + name alignment).

## Why this beats the original recommendation

Original: option B as a single PR. Council showed this was wrong on three independent dimensions (durability, sizing, dead coordination contract). A single PR couldn't have shipped without regressions.

Revised: phased delivery where each phase is independently shippable, has its own test plan, and doesn't depend on speculative future work. Phase 1 stops the noise immediately; Phase 2 fixes the coordination contract for real; Phase 3 completes the architectural separation.

## What's out of scope

- Vigil's own `leave_note` calls at `agents/vigil/agent.py:382, 578` (gov-down, Lumen-unreachable) — these are state transitions of long-running services, not fleet snapshots. Different shape, separate decision.
- Watcher (already uses `post_finding` correctly per `agents/watcher/agent.py:79`)
- One-time archive sweep of existing Sentinel KG entries (operator action, not a contract change)
