# Basin Provenance, Verdict Sources, and Margin Scope

Reference companion to `skills/governance-fundamentals/SKILL.md`. Load when
`policy_basin` differs from `basin`, `risk_score_source` differs from
`current_risk`, `margin_scope` is `measured_edges_only`, or when comparing
check-in `margin` with `recovery.margin.v2`.

## Policy Basin vs. Primary EISV Source

When a response includes `policy_evaluation.inputs.basin`, read it as the
decision-time policy basin. Agent-facing state fields can be sourced from the
primary EISV path for that response, so newer responses also include
`policy_basin`, `policy_basin_source`, and `primary_eisv_source` to make that
measurement distinction explicit. The compact formatter keeps `basin`,
`policy_basin`, and `primary_eisv_source`, and drops `policy_basin_source`.

## Internal Verdicts and Risk Provenance

Separately from the operational `action` directive, `metrics.verdict` may carry
an internal UNITARES verdict such as `safe` / `caution` / `high-risk`. Read it
as interpreted state/context, not as moral judgment.

- Under the default hold (`UNITARES_HOLD_UNREAD_AXES=1`), the Φ cold-start prior
  owns check-ins 1–5; from check-in 6 onward, the behavioral assessment owns the
  main risk/verdict path once `behavioral.confidence >= 0.3` (otherwise, an
  unread state draws `insufficient_reading` with `safe` / risk `0`; with
  `UNITARES_HOLD_UNREAD_AXES=0`, behavioral authority starts at check-in 3).
- Coherence, CIRS, basin, and adaptive-governor compatibility backstops remain
  part of that inference, but they actuate only when the operator sets
  `UNITARES_POLICY_ACTUATION=enforce`.
- On read-only metrics, headline `risk_score` and `status` use the risk that
  actually produced the last verdict when available. Inspect `risk_score_source`:
  `resolved` means that verdict risk, while `phi_history` is the honest fallback
  before an assessment exists. `current_risk` and `mean_risk` remain smoothed Φ
  telemetry and can legitimately disagree with the headline.

## Margin Scope, Unmeasurable Edges, and `recovery.margin.v2`

Two fields ride beside `margin` and qualify what a `comfortable` reading covers:

| Field | Values | Meaning |
|---|---|---|
| `margin_scope` | `all_edges`, `measured_edges_only` | Whether every edge was judged, or only some of them. `compute_proprioceptive_margin` sets it only on `comfortable`/`tight`, while several decision paths re-emit a default of `all_edges`, so an `all_edges` reading beside `settling`/`warning`/`critical` is that default and not a finding that every edge was judged. |
| `unmeasurable_edges` | list of edge names | The edges that had no band to judge against, so they were not assessed at all. |

An edge is unmeasurable when it has no threshold band for this agent. Coherence
is the usual case, and the gate is provenance, not history: the coherence edge
is judged only when `coherence_role` is `behavioral_update_consistency`
(`COHERENCE_INTERPRETABLE_ROLE`), the history window carries that same role, and
at least 10 samples exist. With the deployed `legacy_tanh_v` /
`ode_control_feedback` producer the edge stays unmeasurable no matter how much
history accumulates, so `comfortable` normally arrives as
`margin_scope: measured_edges_only` with `unmeasurable_edges: ["coherence"]`.
Prefer live values from `check_working_state(client_session_id=...)` over
assuming a fixed enum across runtime versions.

Do not transfer check-in `margin` into recovery eligibility. Recovery emits a
separate `recovery.margin.v2` view whose authoritative inputs are risk and
`void_active`; legacy coherence is explicitly excluded diagnostic context. Its
enum is `unknown` / `critical` / `tight` / `comfortable`, with
`nearest_edge` ∈ `void_active` / `risk_score` / `no_risk_authority`.
(On check-in `margin`, `nearest_edge: "oscillation"` occurs when a CIRS
`cirs_block` pause is driven by resonance.)

## Mirror Mode Filtering

The plain-English `mirror` array in a check-in response already summarizes
anything actionable (including a `tight`/`warning`/`critical` margin) — read
that first. In `mirror` mode `margin`/`nearest_edge` are surfaced **only** when
actionable; a `comfortable`/`settling` margin is steady-state and stays out of
the response (the mirror's `"No actionable signals — steady state"` line covers
it).
