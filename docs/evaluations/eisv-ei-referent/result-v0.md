# E and I: where their inputs come from, as deployed (v0)

**Status:** descriptive deployment snapshot, not pre-registered. Window: the 30
days to 2026-10-12 06:30 UTC.
**Script:** [`scripts/analysis/eisv_ei_referent_read.py`](../../../scripts/analysis/eisv_ei_referent_read.py)
(reads frozen snapshots only; never opens a database connection).
**Scope:** the contract
([`eisv-proprioception-contract.md`](../../ontology/eisv-proprioception-contract.md),
*The split*) classes E as externally referenced ("decision outcomes, tool errors
and outcome success are world facts") and gives I's dominant term as
calibration, with I's repair described as repointing its referent at the
external signals E already ingests. Before either axis can be checked against a
referent, the prior question is how much of each, on real check-ins, an external
fact drives. This record answers that question only. It uses no outcome as a
label and makes no outcome claim.

## Summary

- **E moves with the agent's own verdicts.** Within an identity, a median 75% of
  E's variation comes from `decision_history`, the agent's own prior verdict
  actions (72 identities with at least 20 rows; p10 47%, p90 98%). Tool audit and
  externally verified outcomes account for a median of 0%.
- **E's external terms are present but carry almost nothing.** The tool term is
  joined on 99% of check-ins, but `tool_error_rate` was nonzero on 168 of 19,346,
  so it is a near-constant. Externally verified outcomes are a median 0% (mean
  6%) of a check-in's outcome history, which is 80% system-graded
  `trajectory_validated` rows.
- **I's calibration term is one frozen number.** Its 36% effective weight came
  from a single fleet value, 0.1474, identical for every identity on every
  check-in for the whole window, from a lifetime sample count that stayed at 282.
  The per-agent candidate reported `no_data` for all 318 identities with a
  derivation. Its recorders are wired, but every candidate sample in the window
  was gated out (see *Why the calibration bins did not move*).
- **I moves with report-text agreement, not calibration.** A median 87% of I's
  within-identity variation comes from `continuity_I_input`, one minus the
  divergence between self-reported and text-derived complexity.
- **The external supply for a repointed referent is thin but real.** 124 of 359
  identities had any `external_signal` outcome in the window (median 9 each; 35
  had at least 20).

## Method

Every behavioral check-in persists its full derivation in
`state_json.eisv_telemetry.derivation`: each component's value, weight and
source, each adjustment, the outcome rows it read, and the calibration signal.
The script decomposes each persisted E and I into source classes. A component's
effective weight is its weight times the retained weight of every later
adjustment. On all 18,021 rows with a derivation, the class contributions
reconstruct the stored value to within 3.3e-16 and the weights sum to 1.

| Class | E inputs | I inputs |
|---|---|---|
| own verdicts | `decision_history` | |
| legacy ODE | `legacy_coherence_level` | `legacy_coherence_trend` |
| self-report and text | `complexity_calibration`, `continuity_E_input` | `continuity_I_input` |
| fleet calibration | | `calibration_accuracy` |
| outcomes, external / internal | `outcome_success` | `outcome_score_consistency` |
| tool audit | `tool_success_rate` | `unique_tools_ratio` |

Outcome provenance has to come from elsewhere. With
`UNITARES_OUTCOME_PROVENANCE_FILTER` off (the default), `get_recent_outcomes`
selects only `outcome_type, is_bad, outcome_score, ts`, so `verification_source`
is null in every persisted envelope, whatever the real source. A naive read of
the envelopes reports zero external outcomes, which is an instrument artifact.
The script therefore takes a second frozen snapshot of `audit.outcome_events`
over the same window: counts per identity by outcome type and verification
source. Outcome events key on `core.identities.agent_id` and state rows on
`identity_id`, so the export joins through `core.identities`. Each persisted
outcome entry of type `t` is credited as external with that identity's window
share of external `t` rows. That approximates the producer's rolling 24-hour,
20-row window. I's outcome term reads only rows with a finite `outcome_score`, so
its external share is computed over that subset; in this window every persisted
outcome row carried a finite score, so the two shares coincide.

Variance shares are cov(class contribution, value) / var(value) within each
identity with at least 20 rows. They sum to 1 per identity. A class can take a
negative share when its weight shifts as other terms switch on or off; read
them as descriptive.

**Snapshots** (in the operator's analysis directory, not committed; the export
SQL is in the script, `--print-export-sql`):

| File | Rows | sha256 |
|---|---|---|
| `snapshot.jsonl` (behavioral `core.agent_state` derivations) | 19,535 | `7c8d219746a284814cbf54f77bb7b3e4156c708208bff5a9f2d89944a612bbb9` |
| `outcome_provenance.jsonl` (per-identity outcome counts) | 1,491 | `32dedb67c08a08c557c18eaa84e990facc87994c2eabd979705977c198d9f315` |

## Results

Population: 19,535 behavioral check-ins from 359 identities, none synthetic;
rows per identity p50 7, p90 39, mean 54. 1,514 rows persisted no E/I
components, leaving 18,021 rows from 318 identities.

### Effective weight, E (per row, p50; per-identity mean in brackets)

| Class | Weight |
|---|---|
| self-report and text | 0.306 (0.308) |
| own verdicts | 0.238 (0.249) |
| legacy ODE | 0.170 (0.179) |
| tool audit | 0.150 (0.139) |
| outcomes, internal | 0.136 (0.108) |
| outcomes, external | 0.000 (0.017; p90 0.041 per row) |

### Effective weight, I

| Class | Weight |
|---|---|
| fleet calibration | 0.360 (0.377) |
| legacy ODE | 0.216 (0.230) |
| self-report and text | 0.180 (0.169) |
| outcomes, internal | 0.144 (0.114) |
| tool audit | 0.100 (0.093) |
| outcomes, external | 0.000 (0.018; p90 0.043 per row) |

### What moves each axis within an identity (72 identities, p50 [p10, p90])

| Class | Share of E variation | Share of I variation |
|---|---|---|
| own verdicts | 0.751 [0.469, 0.983] | |
| legacy ODE | 0.161 [0.024, 0.271] | -0.069 [-0.404, 0.057] |
| self-report and text | 0.073 [-0.124, 0.371] | 0.866 [0.216, 2.210] |
| fleet calibration | | -0.201 [-0.989, 0.135] |
| tool audit | 0.000 [-0.000, 0.085] | 0.277 [0.002, 0.560] |
| outcomes, internal | 0.028 [-0.320, 0.251] | 0.182 [-0.360, 0.699] |
| outcomes, external | 0.000 [-0.187, 0.173] | 0.000 [-0.427, 0.126] |

I's tool-audit share comes from `unique_tools_ratio`, a usage-breadth measure,
not from tool errors. The fleet-calibration share is not information: the value
never changes, and its share reflects the weight moving when outcome terms switch
on or off.

### Supply

- `audit.outcome_events` for these identities in the window: 68.3%
  `server_observation`, 20.5% `agent_reported_tool_result`, 11.2%
  `external_signal` (3,031 rows).
- Outcome entries persisted in the derivations: 80.3% `trajectory_validated`,
  13.2% `task_completed`, 5.1% `test_passed`, under 1% each for the failure
  types.
- Fleet calibration: one distinct value across 668 hours with check-ins; no hour
  showed two values; sample count 282 on every row.

### Why the calibration bins did not move

A zero can mean the producer never ran, was unreachable, recorded somewhere
else, or ran and admitted nothing. Checked against source and the live state:

- **Wired and reachable.** `calibration_checker.record_prediction` is called on
  the live check-in path (`src/mcp_handlers/updates/phases.py`) and in the
  outcome-event handler (`src/mcp_handlers/observability/outcome_events.py`),
  both passing `agent_id`, which is what fills the per-agent bins (#2194).
- **Recorded where the reader looks.** The persisted live state (`core.calibration`)
  holds strategic bins totalling 282 samples, the same count every check-in
  reported, and zero entries in `bins_by_agent` and `tactical_bins_by_agent`.
- **Ran and admitted nothing.** The check-in path records only outcomes whose
  corroboration weight reaches `TOOL_OBSERVED` (0.65). The outcomes it
  auto-emits are keyword-inferred from the agent's own report; fleet-wide in the
  window, 5,944 task and test outcomes were graded `claim_only` (weight 0.10)
  and 43 `self_report_with_refs` (0.35), none at or above 0.65. The outcome-event
  path also requires a confidence, resolved from a prediction binding, the call's
  confidence argument, or the agent's in-memory monitor. Fleet-wide, 3,331
  outcomes in the window were graded `externally_verified` or `tool_observed`
  (3,209 of them task or test outcomes) and clear the weight gate, but none
  stores a confidence in its detail, and the bins did not grow. The in-memory fallback cannot be ruled out from the database
  alone; the unchanging count on every check-in shows that it added nothing.

So the per-agent repair is not starved of producers but of admissible samples:
the outcomes that carry an agent confidence are self-reports, and the outcomes
that are verified carry no confidence.

## Interpretation

E is classed as externally referenced, but in this window its external terms
carry about 16% of its weight and almost none of its movement. What moves it is
the agent's own verdict history. The verdict path reads E, so this is the
verdict-to-`decision_e` loop the contract already names (the "pause-scores-0.5"
do-not and row 45), measured here as the largest source of E's variation.

The documented reason the outcome provenance filter stays off still holds, and
these numbers reinforce it. Filtering would remove the internal outcome terms,
and the fallback formula raises `decision_history` from 0.35 to 0.40 of E's base
(`get_recent_outcomes` docstring, measured 2026-08-20). The lever is
`decision_history`, not the outcome filter.

I's calibration term carries no current information about anyone: it is the same
frozen number for every agent. The per-agent repair (#2194) is wired but admitted
no sample: confidence and verification never arrive on the same outcome. The
external referent the contract proposes exists for about a third of identities,
mostly in small numbers, and joining it to I needs the agent's confidence at the
time of the verified outcome.

## Limits

- One 30-day window, dominated by scheduled residents (contract, sampling-frame
  note). Identity-level summaries are reported next to row-level ones for that
  reason.
- The outcome split is an approximation by type and identity share, not a
  row-exact join; the envelopes carry no outcome identifiers.
- Variance shares are descriptive decompositions, not causal effects.
- This says where E and I's inputs come from. It does not test whether either
  axis agrees with any referent.

## What this does not authorize

No change to E or I before the registered 2026-12-01 read; both feed its models.
Re-scoring `decision_history`, switching the provenance filter, or swapping I's
calibration source are all predictor changes for that read. The contract's
existing wake rules for `decision_e` apply.
