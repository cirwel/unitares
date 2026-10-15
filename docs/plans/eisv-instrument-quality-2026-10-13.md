# EISV instrument quality: repair and evaluation sequence

**Decision date:** 2026-10-13

**Status:** operator-authorized priority; implementation and study gates below
remain separate.

## Direction

Keep EISV a supported, visible instrument and prioritize its repair and
evaluation. The operator approved this direction after reviewing the October 12
S intervention experiment and the E/I input audit. This changes the ordering in
the [roadmap](../ROADMAP.md); the October 10 decision to move EISV to an optional
module remains in force.

Optionality describes the target runtime dependency. Support includes maintained
readings, source explanations, agent-facing guidance, and a reproducible
evaluation path. Today the check-in still depends on the estimator. The
[boundary proposal](../proposals/active/eisv-core-boundary-v0.md) governs the
ownership move, input capture, parity, and authority stages.

The work serves agents reading their state and operators diagnosing the system.
Prioritizing instrument quality uses maintenance and evaluation effort that
could otherwise go to new integrations; independent-operator validation and the
cross-vendor review study remain on the roadmap. Complete bounded work against
existing evidence before opening another feature workstream.

## Evidence that motivates the work

- [S faithfulness, October 12](../evaluations/eisv-s-faithfulness/result-v0.md):
  synthetic overclaims, confidence collapse, and thrash moved S in the predicted
  direction; accurately reported heavier work moved it little. Most positive
  responses remained when the regime term was held constant. The same experiment
  exposed a regime feedback loop and vocabulary sensitivity. These exploratory
  results characterize the mechanism; they establish neither real-work utility
  nor a validated intervention threshold.
- [E/I inputs, October 12](../evaluations/eisv-ei-referent/result-v0.md): in the
  dated deployment window, prior verdicts dominated E observation variation and
  I's calibration source was frozen. Verified outcomes and stated confidence
  did not supply admissible pairs to the per-agent estimator. The report
  identifies repair targets without testing outcome prediction.
- [Numerical audit, PR #2756](https://github.com/cirwel/unitares/pull/2756): the
  paper proof and the implemented integrator have separate defects. The
  [proposed numerical repair](https://github.com/cirwel/unitares/pull/2757)
  changes the preserved estimator trajectory and is held. Reproducing a numerical
  witness does not establish a failure of the full running governor.

## Execution sequence

| Work | Concrete next deliverable | Boundary for completion |
|---|---|---|
| Protect the measurement path | Finish the existing shadow exception-isolation fix in [#2762](https://github.com/cirwel/unitares/pull/2762), through its owning session and review process. | Injected shadow failures cannot skip the deployed observation handoff. This plan grants no ownership of that PR. |
| Verify the S starting point | Reproduced the existing eight-seed offline run and paired regime ablation on 2026-10-13 at `87dd33c37`; commands and results below. | Reproduction of the existing synthetic fixture; no additional efficacy or confirmatory claim. |
| Specify S's next validation | Draft a protocol covering separate confidence and complexity interventions, vocabulary controls, cadence variation, agent classes, and tool-usage inputs. Include raw drift/complexity baselines to test what the combined reading adds. | Fix the independent unit, seed/stream selection, outcomes, uncertainty reporting, and proposed deciding standard before collection. Operator approval of the standard precedes a confirmatory run. |
| Repair I's evidence supply | Audit the existing confidence capture and prediction/outcome binding with controlled fixtures; specify how an explicit forecast and its verified outcome reach the per-agent estimator. | Missing or mismatched pairs remain missing; a nearby generic confidence statement cannot silently become a task-specific forecast. Live producer or estimator changes require the registered study's preservation review (amended 2026-10-15: under the [stop-rule amendment of 2026-10-15](../proposals/registered/eisv-outcome-grounding-stop-rule-v0.md#amendment-2026-10-15-data-boundary-at-the-first-instrument-change), they record `instrument_change_at` first). |
| Repair E and S feedback | Use the existing offline/shadow comparisons to specify E's verdict-history repair and S's regime-loop repair; report remaining sensitivity and lost information as well as noise reduction. | Keep deployed predictors fixed through the preservation horizon (amended 2026-10-15: until a PR records `instrument_change_at` under the [stop-rule amendment of 2026-10-15](../proposals/registered/eisv-outcome-grounding-stop-rule-v0.md#amendment-2026-10-15-data-boundary-at-the-first-instrument-change)). Any replacement has its own version and an evaluation protocol; it inherits no validity claim from the old instrument. |
| Test agent-facing usefulness | Specify a bounded comparison of source observations alone versus observations plus EISV explanations on matched work. Keep context and review budgets equal and record misleading as well as useful guidance. | A future study of a named consumer and task. It does not follow automatically from faithful measurement and does not authorize pausing agents. |

Work on input audits and protocol drafts can proceed independently. Changing
the live estimator, introducing a new study, and granting intervention authority
are not implied by completing an earlier row. The existing S reproduction is
complete; its follow-up protocol comes before new experimental results.

## Reproduction receipt, 2026-10-13

Ran against `87dd33c37` with Python 3.14.8, eight seeds, and the script's unchanged
hermetic harness. Both commands completed successfully:

```bash
python3 scripts/analysis/eisv_s_faithfulness.py
python3 scripts/analysis/eisv_s_faithfulness.py --paired-ablation
```

Identical-input test-retest matched every field. The reported S responses
reproduce the original result to its displayed precision:

| Intervention | Deployed-path response vs control | Regime-held response vs control |
|---|---:|---:|
| Complexity overclaim | +0.1949 | +0.1921 |
| Confidence collapse with a complexity jump | +0.3075 | +0.2938 |
| Thrash | +0.1570 | +0.1281 |
| Accurately reported heavier work | +0.0069 | -0.0004 |
| Vocabulary-only change | -0.0376 | -0.0003 |

This repeats the same synthetic fixture, including its templated text, absent
tool-usage inputs and outcome history, single agent class, and cadence band.
It supplies no new independent sample or confirmatory result. Every one of the
1,080 intervention-window check-ins in the deployed-path run returned
`safe/proceed`; there was no intervention-benefit test. The confidence-collapse
condition changes claimed complexity too, which is why the follow-up separates
those inputs.

## Preservation and authority

- The feature freeze this plan was written under was lifted by the operator
  on 2026-10-15. This priority change does not by itself authorize new runtime
  feature work: new tools, actions, instruments and model channels stay
  operator-reserved, and the scientific gates below are unchanged.
- The [registered December 1 outcome read](../proposals/registered/eisv-outcome-grounding-stop-rule-v0.md)
  keeps its inputs, selection rules, thresholds, and reporting obligations.
  Do not run ad hoc live discrimination analyses or reinterpret this synthetic
  reproduction as another outcome read.
- The preservation horizon ends when the operator declares the registered
  report final, including correction or reproduction inspection, as specified
  in the boundary proposal. October 22 and December 1 are not automatic release
  dates for the held numerical fix or predictor changes.
  *Amended 2026-10-15:* the horizon no longer gates live-path changes. Under the
  [stop-rule amendment of 2026-10-15](../proposals/registered/eisv-outcome-grounding-stop-rule-v0.md#amendment-2026-10-15-data-boundary-at-the-first-instrument-change), the held numerical fix and predictor changes may land once their PR
  records `instrument_change_at`, a UTC instant no later than their deployment;
  the registered read then admits only outcomes before it. Flag and
  configuration changes count too, and the CI guard does not see them.
- No new pass/fail threshold, fitted estimator, enforcement change, module
  default, or waiver is authorized here. Proposed scientific standards go to
  the operator before use. The boundary proposal's Stage 3 and Stage 4 decisions
  remain separate.
- Evaluate measurement fidelity, advisory usefulness, and intervention benefit
  as distinct claims. Preserve negative and inconclusive results, and retain
  the underlying records regardless of an estimator's verdict.

## Continuing the work

Use the linked result records and existing PRs as the starting points. Record
completed work and evidence in the relevant PR or evaluation record; update this
sequence when those results change the next step. Avoid parallel repairs of the
same surface and repeated model-review rounds for an unchanged artifact.
