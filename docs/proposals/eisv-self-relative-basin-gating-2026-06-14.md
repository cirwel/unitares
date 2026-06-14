# EISV Self-Relative Basin Gating

Status: implemented for issue #689 on 2026-06-14.

## Problem

Self-relative behavioral scoring compares the current EMA-smoothed E/I/S/V
state to each agent's own Welford baseline. For very stable residents, the
baseline standard deviation can collapse to a tiny number because the input is
already EMA-smoothed. On 2026-06-13, Sentinel had an absolutely safe wobble
from its own norm and the self-relative z-scores produced high-risk behavioral
components, which propagated to `cirs_block` and paused the resident for about
18 hours.

PR #686 added `MIN_MEANINGFUL_EISV_STD = 0.05` as a fleet-wide z-score
denominator floor. That stopped the immediate false pause, but it was a blunt
global desensitizer.

## Candidate Designs

### A. Gate self-relative risk by absolute EISV health

Use raw E/I/S/V to decide whether movement from an agent's own norm is allowed
to raise EISV risk. If the current state is absolutely safe, z-deviations are
evidence only and the `low_E`, `low_I`, `high_S`, and `high_V` self-relative
components are zeroed. `adversarial_rho`, `high_CE`, trend handling, and
absolute safety floors stay active.

This is the accepted design.

The scorer deliberately does not call `classify_basin()` and does not use the
full `BASIN_HIGH` shape. Calling `classify_basin()` would be circular because
the behavioral scorer creates the risk input that basin classification uses.
`BASIN_HIGH` is also too strict for this policy surface: its convergence target
shape (`I >= 0.70`, `S <= 0.25`) would classify the captured Sentinel pause
state (`I = 0.6572`, `S = 0.379`) as boundary even though the state was not
dangerous. That would fail the regression.

The non-circular EISV-safe gate is:

- `E >= 0.60`
- `I >= 0.60`
- `S <= 0.50`, or `S <= 0.60` for convergent tasks
- `abs(V) <= 0.15`

### B. Per-dimension EMA-derived denominator floors

A denominator floor can be derived from each dimension's EMA step instead of a
single global constant. This avoids exact-zero/tiny variance blindness outside
the safe gate without applying the old flat `0.05` to every dimension.

This is used only as a secondary guard outside the safe gate:

`min_std = DEFAULT_ALPHAS[dimension] * 0.25`

Inside the safe gate, this guard cannot create risk because self-relative EISV
components are suppressed.

### C. Keep the flat `0.05` floor

Rejected as the long-term rule. It fixes the captured incident, but it still
lets healthy movement from self-baseline count as risk and it applies the same
resolution to dimensions with different EMA alphas.

## Validation Plan

Empirical validation must reconstruct behavioral state from
`core.agent_state.state_json->'behavioral_eisv'`, not from typed column names.
The persisted behavioral blob contains current E/I/S/V, `updates`, `alphas`,
and Welford `baseline_stats`; histories are intentionally absent from DB
snapshots, so validation compares current and proposed scorers under the same
history-free reconstruction.

Checks:

- Captured 2026-06-13 Sentinel state remains below caution/high-risk.
- Synthetic basin-exit state using the same baseline still produces non-zero
  E/I/S/V risk components.
- Latest live baselined fleet rows are compared old-flat-floor vs new-gate for
  risk/verdict/component differences.
- Tight-baseline agents (`std < 0.05` for any E/I/S/V dimension) are reported
  separately because they are the blast-radius population.
