# Does Φ over-flag hard work? Test design (unregistered draft)

Status: proposed, 2026-10-03. **Not registered.** Becomes registerable only
after the operator fixes every value in *Operator choices* below, and may not
read outcome data before the registered 2026-12-01 outcome-grounding read
(see *Sequencing*).
Scope: whether Φ's cold-start verdict separates good from bad outcomes less
well as task complexity rises (over-flagging), versus hard work genuinely
failing more often (Φ correctly flagging). This does not reopen whether
`UNITARES_PHI_TELEMETRY_ONLY` should stay on; that is a recorded operator
policy decision (`eisv-maths-roadmap-v0.md` §8.0, decision 0) and stands on
design grounds whatever this read finds.

## Why this exists

"Φ over-flags hard work" is cited as the reason Φ was demoted to telemetry
(`src/governance_monitor.py` `resolve_verdict_risk` docstring;
`docs/ontology/eisv-proprioception-contract.md`, "#1133 removed phi
over-flagging"). A 2026-10-03 review found no measurement behind it:

- The only empirical support cited for decision 0, the "~1% regression floor",
  was withdrawn on 2026-08-24 as a percentile identity.
- `scripts/analysis/eisv_phi_telemetry_redteam.py` has no outcome labels and
  does not condition on complexity, so it cannot test the effort hypothesis.
- Live 30-day verdicts were safe 17,788 / caution 10 / high-risk 1 (contract, row
  14). The lone high-risk was a complexity *misreport* (self 0.9 vs
  derived 0.23; contract row 25), not effort.
- Mechanism: complexity enters only S (`governance_core/dynamics.py`,
  `+β_c·complexity` and the entropy floor), never V (`V̇ = κ(E−I) − δV`). A
  rough steady-state estimate, not simulated, puts complexity 0.5→1.0 at about
  −0.08 Φ, which stays inside "safe" at the deployed rest.

The claim is therefore **untested**, not refuted. It matters because Φ still
owns the verdict for updates 0–2, and 83% of identities had no more than two
rows at the 2026-08-06 snapshot (contract row 26): for most adopters Φ is the
verdict they see. KG: `2026-10-03T21:13:24.413258+00:00`.

## Sequencing (binding)

The registered stop rule's 2026-12-01 read includes `prior_phi_binned` as a
candidate and draws on the same trusted-anchor outcome labels
(`registered/eisv-outcome-grounding-stop-rule-v0.md`). Querying Φ against those
outcomes before that read would put an analyst's look at Φ–outcome structure
ahead of a read the stop rule keeps from ad hoc re-runs. So:

1. No outcome rows are queried for this design before the 2026-12-01 read
   (including any `-retry-<n>`) has been executed and recorded.
2. Label-free work may proceed now: the coverage diagnostics in step 1 of the
   design below read check-in rows only, never outcomes.
3. Nothing here changes any definition, threshold, cutoff, label rule, or
   reopening condition of the stop rule.

## Design

**Unit.** A check-in at update 0–2 (Φ-owned cold start) on or after migration
040 (`db/postgres/migrations/040_agent_state_epistemic_class.sql`; earlier rows
have no recoverable complexity), paired with a `hard_exogenous` outcome
(`src/mcp_handlers/observability/outcome_events.py`) inside a fixed window.
Auto-check-in outcomes labelled from the same `response_text`
(`src/mcp_handlers/updates/phases.py`) are excluded: label and predictor would
share a source.

**Steps.**

1. *Coverage (label-free, may run now).* Count eligible cold-start check-ins
   per complexity stratum and Φ verdict, by agent.
2. *Selection (after 2026-12-01).* Report the labelling rate per complexity
   stratum before any failure rate. If hard tasks are graded at a different
   rate, the comparison in step 3 is reported as selection-confounded.
3. *Comparison (after 2026-12-01).* Within each complexity stratum, compute
   `P(bad | Φ caution or high-risk) − P(bad | Φ safe)`, with agent-clustered
   bootstrap intervals (one agent's burst is one cluster). Also fit
   `bad ~ Φ + complexity`, clustered by agent.

**Reading.**

- *Over-flagging* is supported if flag rate rises with complexity while the
  bad-rate gap shrinks toward zero in the high stratum.
- *Correct flagging* is supported if bad rate rises with complexity and Φ's
  gap holds across strata.
- A workload–failure association alone validates neither.
- Below the minimum cluster count, the result is **unresolved coverage**, not a
  null (`CLAUDE.md`, *Measurement authority*).

Whatever it shows may inform the cold-start verdict lever. It retires no
capability: Φ remains reported telemetry either way.

## Operator choices (unset; fix before registering)

| Choice | Options raised in review | Value |
|---|---|---|
| Complexity measure | self-reported, grounded/derived, or both | _unset_ |
| Strata | tertiles, or fixed cut points | _unset_ |
| Outcome window after check-in | — | _unset_ |
| Smallest gap difference that matters | e.g. 10 points | _unset_ |
| α and noise floor | — | _unset_ |
| Minimum bad clusters per stratum | ~40 suggested (`eisv_label_power.py`; stop-rule table) | _unset_ |

## Separate open decision: the cold-start prior

Independent of the read above, review raised what should decide updates 0–2.
These are recorded here as options, not a recommendation adopted:

- **Abstain on E/I/S/V, with a drift tripwire.** Report "insufficient
  evidence"; escalate only when ‖Δη‖ is what tips Φ, or on a verification-floor
  hit. Can only de-escalate relative to today. Risk: abstention read as "safe";
  rewards identity resets.
- **Class/cohort prior** (`src/cohort_prior.py`). Risk: scores newcomers
  against statistics they have not earned (that module keeps seeds below the
  z-gate for this reason) and *can* add pauses.
- **Complexity-conditioned prior.** Risk: self-reported, so the agent sets its
  own prior.

The 0.3 warm gate, any drift cutoff, and whether a drift hit is caution or
high-risk are operator choices.

## Do not

- Do not cite "Φ over-flags hard work" as measured until this read, or another,
  supports it.
- Do not query outcome rows for this design before the 2026-12-01 read.
- Do not choose any value in *Operator choices* after seeing outcome data.
