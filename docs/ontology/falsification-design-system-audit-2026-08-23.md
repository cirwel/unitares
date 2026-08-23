# Falsification design system audit — 2026-08-23

**Status:** authoritative inference and protocol correction for the EISV tested-claim
ledger, its recurring ablation reads, and the outcome-grounding stop rule. This
extends the five-claim containment note from 2026-08-22; it does not erase the
underlying source audits, event records, or registered operational decisions.

## Conclusion

The broad negative framing was not supported by a broad body of valid negative
experiments. At the start of this audit, **36 of the 45 numbered claim headings
(80%) contained `REFUTED`**, even though most rows were source inspection,
formula inspection, documentation comparison, path analysis, or a dated
single-deployment census. Those are useful findings, but they are not 36
scientific falsifications of UNITARES.

The outcome-lift evidence is a non-detection from an instrument with about 3%
power against a weak planted effect (true AUC about 0.57), reaching 80% power
only around AUC 0.82 in a deliberately favorable simulation. The individuality
test had an autocorrelation-hostile veto, effective independent n=4, replicated
host identities, and a long wall-clock outage treated as adjacent observations.
The basin and observer-loop reads did not run the required counterfactual.

Accordingly, the current scientific conclusion is **inconclusive or unidentified
for the disputed capability claims**. The durable engineering findings remain:
specific formulas, paths, provenance, documentation, and deployment states were
not what their exact descriptions said.

## A result may earn `REFUTED` only if all six checks pass

| Check | Required question |
|---|---|
| Target | Is the tested estimand the same claim the conclusion names? |
| Counterfactual | Does the design expose the comparison needed for a causal or dynamic claim? |
| Unit | Are uncertainty and resampling based on independent agents or episodes rather than repeated rows, shared hosts, or shared state snapshots? |
| Support and power | Could this sample detect the predeclared smallest scientifically relevant effect with adequate probability? |
| Decision rule | Were the threshold, direction, scope, and stopping rule fixed before the read? |
| Protocol | Were interim accesses, model choices, label changes, and analysis changes prevented or fully disclosed? |

Failure of target, counterfactual, or unit identification yields `UNIDENTIFIED`
or `WITHDRAWN`. Inadequate power yields `INCONCLUSIVE` or `UNTESTED AS DEPLOYED`.
A source contradiction yields a mismatch or path status. None is silently
promoted to a scientific refutation.

## Claim-family review

| Claim family | Design finding | Correct current interpretation |
|---|---|---|
| Individuality of raw behavioral EISV | Leg A rejects synthetic stationary, mean-reverting processes as autocorrelation rises; leg B's seven nominal agents collapse to four independent substrates; a fleet outage is invisible to step-time analysis | `UNTESTED AS DEPLOYED`; the registered operational kill remains honored, but the FAIL is not evidence against the axiom |
| Runtime EMA reference | The registered benchmark cold-started the reconstruction and did not restore the already-warm deployed state | `BENCHMARK FAIL` for that reconstruction; warmed deployed EMA `UNIDENTIFIED` |
| Historical outcome/AUC | Historical all-scope cohort did not match the trusted-anchor target | `WITHDRAWN FOR TARGET INFERENCE` |
| Frozen trusted outcome matrix | 224 rows, 53 bad rows, 16 agents; baseline AUC 0.427–0.435; no slice cleared the selective null; weak-effect power about 3% | descriptive non-detection; no standing AUC bound; `INCONCLUSIVE` for weak/moderate lift |
| Basin guide self-loop | Source excludes the proposed direct same-check-in edge, but the read did not recursively replay ODE, EMA, policy, outcomes, and future decisions | direct path `PATH BOUND`; recursive counterfactual `UNIDENTIFIED` |
| Basin-flip/intervention association | Predictor and response are produced by the same stateful observer; 0.35 is a mixture weight, not a regression coefficient | empirical or causal claim `UNIDENTIFIED`; self-loop and storage-coercion mechanisms remain engineering findings |
| Remaining ledger rows | Most establish deployed formulas, missing inputs, unreachable branches, label provenance, event delivery, or dated population counts | `FORMULA/IMPLEMENTATION/PROVENANCE/DOCUMENTATION MISMATCH`, `PATH BOUND`, `EVENT RECONCILED`, or `DEPLOYMENT SNAPSHOT` |

The detailed five-claim rationale remains in
[`falsification-inference-containment-2026-08-22.md`](falsification-inference-containment-2026-08-22.md).
The outcome harness characterization is in
[`../operations/falsifiability-power-audit-2026-08-23.md`](../operations/falsifiability-power-audit-2026-08-23.md).

## Repeated-read protocol audit

The 2026-07-31 stop rule prohibited ad hoc probe reruns before the registered
2026-12-01 read. Two active six-hour Hermes jobs nevertheless queried the live
outcome-discrimination machinery after the 2026-08-09 frozen cutoff:

| Job | Runs after cutoff | Completed | Failed | What it read |
|---|---:|---:|---:|---|
| `5139fb8f9079`, UNITARES ablation watchdog | 51 | 42 | 9 | outcome inventory plus two live selective-null matrices; tracked thresholds, selected candidates, point estimates, and `NOISE-LEVEL` wording |
| `f8bc3522154e`, UNITARES dogfood/ablation guard | 52 | 43 | 9 | outcome inventory plus two live matrices with null resampling disabled, solely to check lane hygiene |

The second job did not make an inferential report, but it still exposed live
outcome point estimates. Together with the previously paused weekly skeptic
trend, these accesses mean the December run cannot truthfully be described as
the only post-registration data read or as analysis-blind without qualification.

Both jobs were paused on 2026-08-22. The watchdog now fails closed before data
access unless an operator supplies an override whose name and value explicitly
acknowledge protocol contamination. The dogfood guard is being changed to test
the lane contract against synthetic/unit fixtures instead of querying live
outcomes. Outcome-supply inventory may continue because counting support does
not expose discrimination results.

## Consequence for the December stop rule

The fixed date, cohort, thresholds, four PASS conditions, and operational kill
criterion remain in force. Preserving a preregistered decision is different from
overstating what its evidence means.

The December report must:

1. disclose every known interim outcome read and state that the original
   single-read/blinding condition was violated;
2. declare whether any access changed features, labels, scope, thresholds,
   collection, or public narrative;
3. publish read-specific power or a smallest-detectable-effect analysis;
4. treat a support-only failure as insufficient eligible evidence; and
5. treat a support-qualified non-detection as `INCONCLUSIVE` unless the read had
   adequate predeclared power for the smallest relevant effect.

`FAIL` may still close further scheduled work under the operational commitment.
It does not automatically earn `REFUTED` as a scientific status.

## Durable anti-framing rule

Dashboards, papers, automation messages, docs, and shared-memory summaries must
preserve the result class. They may report failures plainly, but must not roll
engineering mismatches, path bounds, snapshots, non-detections, and unidentified
counterfactuals into a project-level count of “negative results.” Any future
`REFUTED` heading must cite the target, counterfactual, independent unit, power,
predeclared rule, and protocol record that earned it.
