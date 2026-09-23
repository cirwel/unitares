# Legacy coherence identity ablation v0

Status: measurement-only proposal
Schema: `legacy_coherence_identity_ablation.v1`

## Decision

Do not recalibrate trajectory-identity thresholds around the current
`coherence` distribution. Record an append-only ablation first, with no effect
on trajectory persistence, similarity, trust tier, risk, verdicts, or alerts.

## Original mathematics and intent

The original scalar is

```text
C(V) = 0.5 * (1 + tanh(C1 * V))
```

It is a bounded, monotone transform of the signed ODE void coordinate `V`.
Around `V = 0`, `C(V) = 0.5`; negative `V` maps below `0.5` and positive `V`
maps above it. This makes the value suitable as directional controller
activation or compatibility feedback. It does not independently measure
health, consistency, recovery, or identity stability.

The repository trace is explicit. `governance_core/coherence.py` locates the
formula in UNITARES v4.1 section 3.4 and places it inside the ODE as stabilizing
feedback. Commit `69ee5a79` later promoted behavioral EISV to the primary
measurement surface but migrated E, I, S, and V only; the public `coherence`
field continued to read the demoted ODE attractor. The current compatibility
name therefore outlived the measurement role that consumers inferred from it.

The later trajectory implementation misread that controller value in three
places:

- dip/recovery cycles in `coherence_history` became `recovery.tau_estimate`;
- the same value became `homeostatic.recovery_tau`;
- low standard deviation became a high `stability_score`, which multiplies
  identity confidence.

A nearly constant producer therefore looks maximally stable and supplies a
default recovery time even when it carries little or no identity information.
That is the signal-degeneracy failure: the implementation rewards the field
for not moving.

## Measurement-only intervention

For each eligible check-in, the persisted EISV telemetry envelope records:

- deployed `recovery_tau`, `stability_score`, and `identity_confidence`;
- the maturity factor `min(1, lifetime_updates / 200)`;
- a candidate in which coherence-only recovery and stability fields are
  unavailable evidence;
- the maturity-only confidence and its delta from deployed confidence.

The maturity-only number isolates the removed multiplier. It is not a proposed
replacement identity score and is never written as a trajectory signature.
Replacing the history with constant `0.5` would be misleading here: it would
produce stability `1.0` and preserve the very degeneracy under examination.

## Deliberate boundaries

The shadow does not model:

- a counterfactual trajectory signature;
- genesis or current signature replay;
- trajectory similarity;
- trust-tier changes;
- risk adjustment;
- coherence-derived ethical-drift/ΔEta dynamics;
- coherence pause, CIRS, basin, or adaptive-governor decisions;
- persistence or alerts;
- recursive baselines or future outcomes.

Those require longitudinal replay because deployed signatures already contain
the legacy fields. A per-check-in arithmetic substitution cannot estimate that
feedback safely.

The behavioral-baseline anomaly consumer is outside this identity shadow and
is retired rather than modeled: z-scoring a compatibility signal with no
meaningful dynamic range would reproduce the category error instead of
producing useful prospective evidence. Its historical Welford statistics stay
available for compatibility and replay but cannot add entropy.

## Evidence needed before migration

Aggregate the append-only shadow by agent maturity and producer provenance.
Before a live change, require:

1. adequate eligible coverage across long-lived and newly onboarded agents;
2. replay of genesis/current signature pairs with coherence-only components
   excluded rather than zero-filled;
3. comparison of similarity, trust-tier, and risk-adjustment distributions;
4. explicit replacement measurements for recovery and stability, or a decision
   to leave those dimensions unknown;
5. a versioned migration plan that does not compare new signatures directly to
   legacy baselines without provenance-aware normalization.

Until those conditions hold, the deployed identity path remains unchanged and
the shadow remains measurement-only.
