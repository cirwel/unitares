# Broadened Tactical Truth Channel + Per-Channel Calibration

**Date:** 2026-04-26
**Status:** SHIPPED — `HARD_EXOGENOUS_TYPES` lives in `src/mcp_handlers/observability/outcome_events.py`; `tactical_bin_stats_by_channel` lives in `src/calibration.py`. Companion plan was retired in PR #334 once code shipped.
**Author:** Claude (with Kenny)
**Revision:** 2 (incorporates dialectic-knowledge-architect, feature-dev:code-reviewer, and live-verifier feedback)

## Problem

`check_calibration` returns `calibrated: false` despite `trajectory_health: 0.978` and `accuracy: 1.0`. The dashboard surfaces this as a contradictory "Calibration: No · 97.8% trajectory" card.

Live data confirms the read is honest. Mean stated confidence is **0.6345** across 12,042 self-reports, while empirical accuracy on the tactical truth channel is **1.0**. Per-bin calibration error reaches **0.72** in the lowest-confidence bin.

The structural cause is the truth-channel definition. Two functions in `src/mcp_handlers/observability/outcome_events.py` jointly gate what reaches tactical calibration:

- `_classify_hard_exogenous_signal` (lines 33–40) maps `test_passed`/`test_failed` → `"tests"`; other outcome types fall through to a `detail`-key check (commands, files, lint, tool_results).
- The inline call at line 266 (`if outcome_type in ('test_passed', 'test_failed'):`) drives `CalibrationChecker.record_tactical_decision`.

`tests` are a high-prior channel — they are usually only run when expected to pass — so empirical accuracy on this channel is pinned at 1.0 (2,298 events, 0% bad_rate over 30 days). With confidence reports averaging 0.63 against an effectively-1.0 truth signal, calibration will read "miscalibrated → under-confident" indefinitely regardless of actual agent behavior.

The metric is correct about what it measures. The truth channel is too narrow to support the conclusion the metric *appears* to be drawing.

## Goals

1. Restore an honest tactical calibration signal by broadening the truth channel beyond a single high-prior source.
2. Surface *which* channel a miscalibration originates in, instead of a single Yes/No that the operator cannot interrogate.
3. Mark the definition change in the audit trail so historical calibration state is not silently re-interpreted.

## Non-Goals

- Re-grounding strategic (`trajectory_health`) calibration. That signal is already healthy at 0.978.
- Redefining what "confidence" means in agent self-reports.
- Auto-tuning agent confidence based on observed gaps. Belongs to a separate calibration-feedback loop.
- Per-channel *expected confidence baselines* (priors). Acknowledged below as a follow-up; this PR ships per-channel reliability without per-channel priors.

## Framing — Decomposition vs. Sprawl

This change adds a per-channel *partition* of an existing metric along its causally-relevant axis (the `outcome_type` taxonomy). It is metric **decomposition**, not new EISV surface area: channels are an existing typed concept in the schema, not invented here. The user's standing rule against EISV-surface sprawl (`feedback_eisv-surface-sprawl`) targets non-isomorphic *new* signals; conditioning an existing signal on an axis it was already silently aggregating over is the move that makes a degenerate aggregate diagnostic, not the move that creates sprawl.

## Live-State Evidence

`audit.outcome_events`, last 30 days, all agents:

| outcome_type | n | bad_rate | avg_score | reconstructable confidence | eligible today? |
|---|---|---|---|---|---|
| trajectory_validated | 19,674 | 2.1% | 0.502 | n/a | strategic only |
| test_passed | 2,300 | 0.0% | 1.000 | **0%** (0/2,300) | yes (tactical) |
| task_completed | 1,532 | 0.0% | 0.838 | 57% (872/1,532) | **no** |
| task_failed | 26 | 100% | 0.327 | 100% (26/26) | **no** |
| cirs_resonance | 16 | 100% | 0.000 | n/a | no |

Adding `task_*` produces ~1,558 backfillable historical samples with non-zero failure base rate, plus future inflow. `test_passed` rows in current production carry no `reported_confidence` in `detail`, so backfill of the existing `tests` channel is structurally impossible — the `tests` channel can only accumulate forward from this PR's deployment.

Current epoch state (`core.epochs`):

| epoch | started | reason |
|---|---|---|
| 1 | 2026-03-16 | initial epoch |
| 2 | 2026-03-29 | behavioral EISV replaces ODE dynamics |

This PR bumps to **epoch 3**.

## Design

### 1. Whitelist broadening — two coupled changes

Single source of truth as a module-level constant in `src/mcp_handlers/observability/outcome_events.py`:

```python
# Hard-exogenous outcome types eligible for tactical calibration.
# Must be binary pass/fail from real work — not graded scores, not retroactive.
HARD_EXOGENOUS_TYPES = frozenset({
    'test_passed', 'test_failed',
    'task_completed', 'task_failed',
})

_HARD_EXOGENOUS_TYPE_TO_CHANNEL = {
    'test_passed': 'tests', 'test_failed': 'tests',
    'task_completed': 'tasks', 'task_failed': 'tasks',
}
```

`_classify_hard_exogenous_signal` (lines 33–40) is rewritten to consult the constant, so the routing function and the whitelist cannot drift apart:

```python
def _classify_hard_exogenous_signal(outcome_type: str, detail: Dict[str, Any]) -> str | None:
    channel = _HARD_EXOGENOUS_TYPE_TO_CHANNEL.get(outcome_type)
    if channel:
        return channel
    for key, label in _HARD_EXOGENOUS_DETAIL_KEYS:
        if detail.get(key):
            return label
    return None
```

The inline tuple at line 266 (`if outcome_type in ('test_passed', 'test_failed'):`) is replaced with `if outcome_type in HARD_EXOGENOUS_TYPES:`. Both gates now derive from the same constant.

`cirs_resonance` is **not** added. Council reasoning (corrected from the v1 spec): cirs_resonance is a *detector* output, not a *prediction* outcome — calibration asks "did your stated confidence match reality," and cirs_resonance events have no stated-confidence anchor. It is not "thin and pinned"; it is the wrong shape for a calibration signal.

### 2. Per-channel calibration breakdown

The bin-level per-channel work lives on `CalibrationChecker` in `src/calibration.py`, where `tactical_bin_stats` already lives. `SequentialCalibrationTracker` keeps its `signal_sources` count dict as-is (used for the hygiene guard in §5).

`CalibrationChecker` additions:

```python
# Existing
self.tactical_bin_stats: defaultdict[str, dict] = defaultdict(...)

# New (additive, parallels existing structure)
self.tactical_bin_stats_by_channel: defaultdict[str, defaultdict[str, dict]] = (
    defaultdict(lambda: defaultdict(lambda: {
        "count": 0, "predicted_correct": 0, "actual_correct": 0,
        "confidence_sum": 0.0,
    }))
)
```

`record_tactical_decision` gains a `signal_source: str | None = None` param. When provided, the same row update happens against `tactical_bin_stats_by_channel[signal_source][bin_key]` *in addition to* the aggregate `tactical_bin_stats[bin_key]` — aggregate is preserved for back-compat.

New method:

```python
def compute_tactical_metrics_per_channel(self) -> Dict[str, Dict[str, CalibrationBin]]:
    """Return {channel: {bin_key: CalibrationBin}} for surfaced channels."""
```

State persistence in `_serialize`/`load_state` (lines ~787, ~845 of `calibration.py`) extends to round-trip `tactical_bin_stats_by_channel`. Existing state files lacking the key load with `tactical_bin_stats_by_channel = {}` — additive, no breakage.

`SequentialCalibrationTracker` schema also gains an `epoch` field (top-level in serialized JSON) for the migration logic in §3:

```python
"epoch": 3
```

`src/calibration.py::check_calibration` adds a `per_channel_calibration` key:

```python
result["per_channel_calibration"] = {
    "tests":  {"calibrated": bool, "samples": int, "calibration_gap": float, "issues": [...]},
    "tasks":  {"calibrated": bool, "samples": int, "calibration_gap": float, "issues": [...]},
}
```

Aggregate `calibrated`/`accuracy`/`tactical_evidence` keys are unchanged. Verified additive: `loadCalibration` in `dashboard/dashboard.js:1196` reads only the named keys it knows about; no caller relies on field count or schema strictness.

### 3. Epoch bump (2 → 3) via canonical script

Use `scripts/dev/bump_epoch.py`:

```bash
python3 scripts/dev/bump_epoch.py --reason "broadened tactical calibration truth channel — task_* added; per-channel surface in API"
```

This updates `config/governance_config.py::CURRENT_EPOCH`, inserts the row in `core.epochs`, and clears stale baselines. The `core.epochs` table currently records the v2.9.0 cautionary tale: "v2.9.0 commit cbaaed95 bumped CURRENT_EPOCH in config but bypassed bump_epoch.py, so the core.epochs INSERT never ran." This PR must not repeat that pattern.

State-file migration in `SequentialCalibrationTracker.__init__` for `data/sequential_calibration_state.json`:

```python
from config.governance_config import GovernanceConfig

if state.get("epoch", 1) != GovernanceConfig.CURRENT_EPOCH:
    archive_path = self.state_file.with_suffix(f".bak.epoch{state.get('epoch', 1)}")
    try:
        self.state_file.rename(archive_path)
    except FileNotFoundError:
        pass  # concurrent migration won the race; safe to no-op
    state = self._fresh_state()  # epoch = CURRENT_EPOCH
    logger.warning("Calibration epoch changed; archived prior state to %s", archive_path)
```

The `FileNotFoundError` fallback handles the narrow concurrent-process window (server restart + backfill running simultaneously). Single-process restarts are sequential and unaffected.

The strategic state file `data/calibration_state.json` is not migrated by this PR — strategic calibration is out of scope. Its epoch handling, if any, remains unchanged.

### 4. Backfill — task channel only

`scripts/dev/backfill_tactical_calibration.py`:

```
Usage: backfill_tactical_calibration.py [--dry-run] [--days 30]
```

Behavior:
1. Verify `data/sequential_calibration_state.json` epoch matches `GovernanceConfig.CURRENT_EPOCH`. Exit non-zero with instructions if not — caller must restart the server once first to trigger migration.
2. Query `audit.outcome_events`:
   ```sql
   SELECT ts, outcome_type, agent_id, is_bad,
          (detail->>'reported_confidence')::float AS confidence
   FROM audit.outcome_events
   WHERE outcome_type IN ('task_completed', 'task_failed')
     AND epoch = (SELECT MAX(epoch) FROM core.epochs)
     AND ts > NOW() - INTERVAL '<days> days'
     AND detail->>'reported_confidence' IS NOT NULL;
   ```
3. Replay each row through `sequential_calibration_tracker.record_exogenous_tactical_outcome(..., persist=False)`.
4. Call `tracker.save_state()` exactly once after the loop completes successfully. On any DB error, exit non-zero without calling `save_state()` — state file is not partially mutated.
5. Emit summary: candidates / replayed / skipped (per skip reason).

`test_*` is not backfilled — current production rows carry no `reported_confidence` in `detail`. The `tests` channel accumulates forward from deployment.

### 5. Reporting-hygiene guard

After deployment, the `tasks` channel's calibration capacity rests on `task_failed` events being reported truthfully. If `task_failed` reporting drops, the channel collapses back to a high-prior pin (same pathology as today's `tests`).

Add to `check_calibration` response when per-channel data exists:

```python
"per_channel_health": {
    "tasks": {"bad_rate_30d": 0.017, "bad_rate_pinned_to_zero": False, ...},
    "tests": {"bad_rate_30d": 0.000, "bad_rate_pinned_to_zero": True, ...},
}
```

`bad_rate_pinned_to_zero` flips True when a channel's rolling 30-day bad_rate is exactly 0.0 with ≥100 samples. Sentinel can subscribe to this and emit an anomaly when a previously-non-zero channel pins.

### 6. Dashboard surfacing

`dashboard/dashboard.js::loadCalibration` (line 1196) extends:

```js
if (result.per_channel_calibration) {
    var channels = result.per_channel_calibration;
    var chips = Object.entries(channels).map(([name, c]) => {
        var icon = c.calibrated ? '✓' : '✕';
        return name + ': ' + icon;
    }).join(' · ');
    detailEl.textContent = samples + ' samples · ' + chips;
} else {
    // existing rendering preserved
}
```

Aggregate Yes/No headline at line 1219 is unchanged. Modal click expands to full per-channel reliability table.

### 7. Paper note — same PR

Add a paragraph to `unitares-paper-v6/sections/12_limits.md` (or §11.6 / §12.4 area, wherever calibration grounding is discussed):

> "Tactical calibration in v6.x was grounded against `test_*` outcomes only. As of epoch 3 (2026-04-XX), the truth channel is broadened to `test_* ∪ task_*` and the API surfaces per-channel reliability. The narrow `test_*` channel was structurally biased toward an under-confidence read because tests are a high-prior signal (run only when expected to pass). The broadened channel pairs a high-prior source (task_completed) with a real-failure source (task_failed) so the empirical-accuracy denominator is no longer pinned at 1.0."

The paper update ships in **this PR**, not deferred. Per `feedback_eisv-bounds-drift`: papers are source of truth; runtime change must not lead the paper.

If the paper PR can't land in the same window, the runtime change ships behind a feature flag (`UNITARES_BROADENED_CALIBRATION=1`) defaulted off until the paper merges.

## Components & Boundaries

| Unit | Responsibility | Talks to |
|---|---|---|
| `outcome_events.py` (`_classify_hard_exogenous_signal` + line-266 gate) | Decides which event types feed tactical calibration; both gates derive from `HARD_EXOGENOUS_TYPES` | `calibration_checker`, `sequential_calibration_tracker` |
| `CalibrationChecker` | Owns `tactical_bin_stats_by_channel`; new `compute_tactical_metrics_per_channel`; `record_tactical_decision` accepts `signal_source` | `data/calibration_state.json` |
| `SequentialCalibrationTracker` | Owns per-channel `signal_sources` counts (existing); handles epoch migration on `__init__`; reports `bad_rate_pinned_to_zero` | `data/sequential_calibration_state.json` |
| `CalibrationChecker.check_calibration` | Composes aggregate + per-channel + per-channel-health response; aggregate fields unchanged | self + `SequentialCalibrationTracker` |
| `backfill_tactical_calibration.py` | One-shot replay of `task_*` rows from epoch-current `audit.outcome_events` | `audit.outcome_events`, `sequential_calibration_tracker` |
| `dashboard.js::loadCalibration` | Renders aggregate headline + per-channel chips | `/v1/tools/call check_calibration` |

## Error Handling

- **Backfill on missing confidence**: skip row, increment `skipped_no_confidence` counter, continue.
- **Backfill DB unavailable mid-run**: exit non-zero before `save_state()`. State file unchanged.
- **Epoch state corrupted**: archive to `.bak.corrupted.<timestamp>`, start fresh.
- **Concurrent migration race**: `FileNotFoundError` on `rename` is treated as "concurrent process already migrated"; no-op and continue.
- **Per-channel breakdown disabled by feature flag**: `check_calibration` omits `per_channel_calibration` and `per_channel_health` keys; dashboard falls back to existing render path.

## Testing

- **Unit** `tests/test_outcome_events_classification.py`:
  - `_classify_hard_exogenous_signal("task_completed", {})` returns `"tasks"`.
  - `_classify_hard_exogenous_signal("task_failed", {})` returns `"tasks"`.
  - `_classify_hard_exogenous_signal("test_passed", {})` returns `"tests"`.
  - `_classify_hard_exogenous_signal("cirs_resonance", {})` returns `None`.
  - `HARD_EXOGENOUS_TYPES` and `_HARD_EXOGENOUS_TYPE_TO_CHANNEL` keys agree.
- **Unit** `tests/test_sequential_calibration.py`:
  - `compute_metrics_per_channel` returns correct bin stats given multi-channel synthetic input.
  - Epoch mismatch in state file triggers archive + reset on `__init__`.
  - State-file write/read round-trip preserves `per_channel_bin_stats` and `epoch`.
  - `bad_rate_pinned_to_zero` flips True at ≥100 samples + 0.0 bad_rate.
- **Unit** `tests/test_calibration_checker.py`:
  - `check_calibration` includes `per_channel_calibration` and `per_channel_health` when sequential tracker has multi-channel state.
  - Aggregate `calibrated` field is unchanged when only one channel has data.
- **Integration** `tests/integration/test_outcome_event_calibration_wiring.py`:
  - `outcome_event(outcome_type="task_completed", arguments={"reported_confidence": 0.8})` populates `tasks` channel in tactical state.
  - `outcome_event(outcome_type="cirs_resonance", ...)` does NOT populate tactical state.
- **Script** `tests/test_backfill_tactical_calibration.py`:
  - Dry-run on fixture rows reports correct candidate / replay / skip counts.
  - Live-run mutates state file to expected snapshot, calling `save_state()` exactly once.
  - DB-error mid-replay leaves state file unchanged (verify via mtime / hash).

## Open Questions (runtime, not design)

1. **Confidence reconstruction coverage on tasks**: 57% on `task_completed`, 100% on `task_failed`. Backfill summary will report exact counts after first run.
2. **Whether to alert on `tests` channel pinning**: the `tests` channel still has the original high-prior pathology, but we're keeping it because forward-flow may eventually carry confidence (if instrumentation improves). Sentinel will surface it via `bad_rate_pinned_to_zero` and the operator decides.

## Out-of-Scope Follow-Ups

- **Per-channel expected-confidence baselines** (priors): per-channel reliability without per-channel priors is half a step. An agent stating 0.6 for a test (which they only run when expected to pass) is rational; stating 0.6 for a task is a different statement. Future PR adds `expected_confidence_prior[channel]` and reports calibration deviation from the prior, not from a uniform 0.5 anchor.
- Add `dialectic_resolution` outcomes to the tactical channel once their volume justifies a separate per-channel surface.
- Per-agent calibration breakdown (currently fleet-wide; requires more per-agent volume).
- Promote `signal_sources` from a count dict to a typed channel registry so future channels declare themselves.
