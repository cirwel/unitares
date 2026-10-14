# Coherence gate and legacy-coherence identity shadow reads — 2026-10-14

Two shadows that had been computed on every check-in but never read were
read once, over a 30-day window, against the single-operator deployment
database. Both reads are descriptive. Neither queried outcome events, and
nothing here touches the registered 2026-12-01 read or its inputs
(`docs/proposals/registered/eisv-outcome-grounding-stop-rule-v0.md`). No
live flag, threshold, verdict or actuator changes. The record of the run is
knowledge-graph finding `2026-10-14T19:44:34.905314+00:00`.

## Coherence gate shadow

Step 4 of `docs/proposals/active/coherence-proprioceptive-thresholds-v0.md`
section 6, run with `scripts/analysis/coherence_gate_shadow_read.py
--window-days 30` on master `ccd802e`.

- 35,779 rows, all statistic version `behavioral_v_recent_two_sided_v2`.
  Eligible 29,882; ineligible 5,482 (`behavioral_baseline_immature`) and 415
  (`insufficient_recent_history`).
- Scale source among eligible rows: floor 27,517, sample standard deviation
  2,365.
- `would_action` was `proceed` on every eligible row. `|z|` p50 0.147, p90
  0.946, p99 1.907, max 2.938.
- k sweep: 234 rows at `|z| >= 2.0` (0.78%), 28 at 2.5, none at 3.0
  (`k_pause`), none at 4.0 (`k_block`) or 5.0 (`k_floor`).
- Agreement with the attributable fleet gate: 29,882 of 29,882.
- Positive control (`--positive-control`, no database): 12 of 12 scenarios
  PASS. Every tier fires from in-domain states in both scale regimes, the
  immature-baseline and short-history edges report ineligible, and each k is
  reachable against `(V_MAX - V_MIN) / floor`.

What this establishes: the instrument can fire, and over this window the
traffic never reached the pause tier. The script's own caveat stands:
reachability is necessary, never sufficient, and a k selected because traffic
was quiet at it still passes the control. Silence here is quietness over 30
days of this fleet, not efficacy and not an outcome claim.

Traffic concentration matters for any threshold read off these rows. One
agent contributed 13,913 of the 29,882 eligible rows and holds the maximum
`|z|`; three agents contributed about 26,000. Per-agent accounting, as the
roadmap requires for independent draws, applies before any k is moved.

## Legacy-coherence identity shadow

`docs/proposals/active/legacy-coherence-identity-ablation-v0.md` recorded
(2026-09-23) that the shadow is computed on every check-in
(`compute_legacy_coherence_identity_shadow`, called from
`src/mcp_handlers/updates/phases.py`) but that its aggregation read had not
been done. This is that read: one SQL aggregation over
`core.agent_state.state_json` at
`eisv_telemetry.shadow_ablations.candidates.legacy_coherence_neutralized.trajectory_identity`,
schema `legacy_coherence_identity_ablation.v1`, rows recorded in the 30 days
to 2026-10-14. No script was added; the query is reproduced below.

- First shadow row: 2026-09-14. In the window: 35,665 rows carry the shadow,
  32,100 eligible, 3,565 `insufficient_trajectory_history`. 938 agents carry
  it; 196 are eligible.
- Deployed `stability_score`: p50 0.9996, p90 0.9999, minimum 0.9564; 92.8% of
  rows at or above 0.99. Per-agent standard deviation (agents with at least
  five eligible rows): p50 0.0048. `maturity_factor` is 1.0 on 86.6% of rows.
- `candidate_minus_deployed.identity_confidence_without_legacy_stability_multiplier`:
  p50 0.0004, p90 0.0036, maximum 0.0195. No agent moves by more than 0.05 on
  any row.

What this establishes: the proposal's premise holds on live data. The legacy
stability term is close to a constant 1 across the eligible fleet, so it adds
almost no information to identity confidence, and removing it as similarity
evidence would change that confidence by under 0.02 everywhere observed. Of
the proposal's five pre-migration conditions this covers part of condition 1
(coverage, skewed toward long-lived agents: 196 eligible of 938) and condition
3 for one distribution (identity confidence). Trajectory similarity, trust
tier and risk adjustment are listed by the shadow itself as `not_modeled` and
remain unmeasured. The deployed identity path is unchanged.

```sql
WITH t AS (
  SELECT s.identity_id,
    (s.state_json #>> '{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,trajectory_identity,deployed,stability_score}')::float AS stab,
    (s.state_json #>> '{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,trajectory_identity,candidate_minus_deployed,identity_confidence_without_legacy_stability_multiplier}')::float AS delta,
    (s.state_json #>> '{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,trajectory_identity,deployed,maturity_factor}')::float AS mat
  FROM core.agent_state s
  WHERE s.recorded_at > now() - interval '30 days'
    AND s.state_json #>> '{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,trajectory_identity,eligible}' = 'true'
)
SELECT count(*), count(DISTINCT identity_id),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY stab),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY delta),
       max(delta), avg((mat >= 1.0)::int)
FROM t;
```

## Not run

`scripts/analysis/legacy_coherence_dependency_shadow.py` joins the
behavioral-sensor shadow to `audit.outcome_events`. That is an outcome read,
and the registered stop rule asks that no ad hoc outcome analysis run before
2026-12-01. It was left for the registered protocol or an operator decision.

## Reproduce

On the deployment host, from the repository root, with the server's
interpreter (the repository `.venv` lacked `asyncpg` on 2026-10-14):

```bash
python3 scripts/analysis/coherence_gate_shadow_read.py --positive-control
python3 scripts/analysis/coherence_gate_shadow_read.py --window-days 30
psql -d governance -X -c "<query above>"
```
