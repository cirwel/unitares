# Legacy-coherence dependency ablation v0

Status: prospective shadow contract, 2026-08-12

Scope: the legacy `C(V_ODE)` contribution to behavioral E/I and the omitted-
confidence fallback. No live weights, thresholds, verdicts, or actuators change.

## Decision

Record a midpoint-neutralized candidate beside the deployed calculation. The
candidate answers one narrow question: how much does variation in the legacy
directional controller move the current raw measurement? It does not pretend to
be a replay of the agent's alternative history.

This is the next step required by
`coherence-proprioceptive-thresholds-v0.md`: shadow the E/I sensor before moving
the distribution beneath learned baselines. It is a new prospective measurement
premise and therefore remains separate from the pre-registered 2026-12-01
general EISV outcome matrix in `eisv-outcome-grounding-stop-rule-v0.md`.

## Exact intervention

The deployed behavioral sensor reads the last ten legacy coherence values.

- E maps their mean from `[0.35, 0.65]` to `[0.3, 0.9]` and gives that term
  25% weight when outcomes are available or 30% otherwise.
- I maps the split-half coherence trend to `[0.3, 0.9]` and gives that term
  30% weight when outcome consistency is available or 40% otherwise.
- Omitted-confidence fallback gives the current coherence value 55% of its
  compatibility base.

The shadow replaces the relevant coherence values with `0.5`, the equilibrium
of `C(V) = 0.5(1 + tanh(C1*V))`. Under the deployed mappings this produces an E
level component of `0.6` and an I trend component of `0.6`. The 55% confidence
term becomes the fixed intercept `0.5 * 0.55`. Every other input, weight,
penalty, clamp, and deterministic offset stays identical.

This removes information carried by the controller's variation while retaining
the central compatibility intercept. It is not a reweighting experiment.

## Telemetry contract

Each new `eisv.telemetry.v1` envelope contains an additive
`shadow_ablations` object with schema `eisv.shadow_ablations.v1`.

The candidate is named `legacy_coherence_neutralized` and may contain:

- `behavioral_sensor`: paired deployed/candidate raw E and I plus signed deltas;
- `derived_confidence`: paired deployed/candidate omitted-confidence fallback
  plus signed deltas;
- intervention provenance and an explicit `policy_applied: false` marker;
- `not_modeled` boundaries.

The envelope is append-only telemetry. Compact summaries expose only schema,
recorded, and eligibility fields. No shadow value is read by measurement,
policy, enforcement, calibration history, or baseline updates.

## Counterfactual boundary

The current candidate is a one-check-in measurement ablation. It does not model:

- recursive E/I histories;
- behavioral EMA or Welford-baseline evolution;
- a counterfactual V trajectory;
- calibration-history or entropy feedback;
- policy decisions or actuator effects;
- future outcomes.

Consequently, a large one-step delta is evidence of causal dependency, not
evidence that the candidate would create the same long-run delta. A small delta
also does not establish safety after recursion.

## Prospective safety read

`scripts/analysis/legacy_coherence_dependency_shadow.py` is the only planned
outcome-linked read for this experiment. Its defaults are fixed before shadow
data exists:

1. trusted `external_signal` task/test outcomes only;
2. controlled validation fixtures excluded;
3. the latest non-synthetic prior state at least 30 minutes before the outcome;
4. exact paired deployed/candidate rows from the same measurement envelope;
5. clusters keyed by `(agent, measurement_id)` so repeated labels on one state
   do not manufacture independence;
6. direct bad-outcome ranking by `1 - E`, `1 - I`, and `1 - confidence`, with no
   best-channel selection;
7. outcome statistics withheld until a channel has at least 150 independent bad
   clusters;
8. a paired cluster-bootstrap 95% interval for candidate-minus-deployed AUC;
9. AUC non-inferiority margin `-0.05`, inherited from the standing bound on
   operationally relevant lift rather than chosen after seeing these data.

**Note, 2026-09-02, no contract change.** Item 2 inherited the structural
fixture predicate at registration, two days after `calibration_excluded`
(which the writer also stamps for a scraped confidence) entered that
predicate's fixture set, so under item 2 as registered every trusted row whose
confidence the server had scraped is excluded; the read had joined 0 outcomes
when this was found. Item 2 keeps its registered meaning: the read's
`--fixture-rule` flag defaults to `registered`. Running it with `corrected`
keeps rows whose only exclusion is a scraped confidence and is a deviation
from this contract that the report names and that must be disclosed here if
its result is ever cited. Record: `outcome-fixture-conflation-decision-packet-v0.md`.

Confidence Brier scores are reported as a calibration diagnostic, but this
contract deliberately does not invent a post-hoc Brier promotion margin. A
confidence-formula change requires that margin to be registered before its first
eligible decision read.

Outcome non-inferiority is a safety check, not the definition of EISV quality.
EISV remains proprioceptive state estimation rather than an outcome oracle.

## Promotion gates

This shadow cannot promote itself.

- Behavioral E/I can advance only if both fixed channels clear the prospective
  AUC safety gate and a separate recursive replay covers E/I history, EMA,
  baselines, V, and policy crossings.
- Omitted confidence is evaluated independently and requires a pre-registered
  calibration rule in addition to its AUC safety read.
- Any proposed live replacement must introduce its own formula version and
  baselines; it must not silently reuse thresholds learned on the mixed-
  provenance distribution.
- Failure or inconclusive evidence leaves the deployed compatibility path in
  place with its provenance warning. It does not justify recalibrating a gate
  against the near-static controller signal.

## Run

```bash
python3 scripts/analysis/legacy_coherence_dependency_shadow.py \
  --scope task --window-days 365 --lead-minutes 30 --resamples 2000
```
