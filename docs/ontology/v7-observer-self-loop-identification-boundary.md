# v7 empirical section: the observer's undeclared self-loop

**Status:** mechanism and artifact findings retained; empirical interpretation
reclassified 2026-08-22. The 2026-08-13 source audit established the topology
and storage defect. It did not identify the registered association or a causal
effect. This note is governed by
[`falsification-inference-containment-2026-08-22.md`](falsification-inference-containment-2026-08-22.md).

**One sentence:** the registered v7 question — *do agents whose basin
assignment flips also receive governance interventions?* — is unidentified by
the proposed regression because both variables are generated inside the same
endogenous observer; v7 may report the verified topology and measured storage
artifact, but not a negative association, causal effect, or known coefficient.

---

## 1. The corpus-maturity blocker is cleared, and clearing it settled nothing

The blocker's own heuristic (~3–4 months of post-epoch data, ~100+ flips) is
satisfied on epoch 3 (started 2026-04-27, `core.epochs`): the clean slice
(synthetic, residents, archived excluded) holds ~9.7k states, ~1.1k agents,
~3.0k consecutive-row regime changes. Thirty times the volume target.

The prior epoch-2 baseline (2026-04-19: 1,026 states / 56 agents / 13 flips) is
**not comparable and must not be cited as a trend anchor**: epoch 2 was
backfilled on 2026-04-20 — the day after that measurement — and today's epoch 2
holds exactly one row per identity, so it contains zero consecutive pairs. The
April numbers describe a population that no longer exists in the table.

## 2. The proposed correlation is endogenous, not an arithmetic identity

The intended regression:

- **X:** basin (regime) flips per agent
- **Y:** governance interventions (`circuit_breaker_trip`, `lifecycle_paused`,
  `anomaly_detected`) per agent

The wiring (all paths verified in source at `origin/master`, 2026-08-13):

```
regime label change
  → _regime_instability(regime_history)      src/behavioral_sensor.py
  → 0.35 weight in S                          raw = 0.40·drift + 0.35·regime + 0.25·cd
  → EISV → risk_score → verdict
  → pause path emits the Y events             src/agent_loop_detection.py (~438)
```

X is one input to a nonlinear, thresholded, stateful pipeline that may produce
Y. That makes the proposed correlation endogenous and unsuitable as external
validation. It does **not** make the event count Y an arithmetic quantity that
contains X. The 0.35 value is a mixture weight inside behavioral S, not a
regression coefficient, closed-loop gain, or causal effect size. No portion of
the empirical effect is computable from the mixture weights alone.

The verification catalogued **every distinct reason string ever recorded** for
`circuit_breaker_trip` and `lifecycle_paused` (not a sample): energy-integrity
imbalance (274), high-risk verdict (119), CIRS resonance (88), CIRS risk
ceiling (26), low basin (12). 100% EISV/CIRS/risk/coherence-derived; zero
operator, timeout, transport, or external-signal triggers in the entire
history. `anomaly_detected` payloads are risk_score deltas ("Risk increased
from X to Y") — same family.

## 3. The instrument was also measuring itself

Independently of the self-loop, 31% of the measured flips (936 of ~3,001) were
manufactured by a storage defect: the live detector emits five basins but the
schema admitted four, so `TRANSITION` (recovering) was silently coerced to
`'nominal'` on every check-in for the entire history of the table — 1,374 rows,
against 37 genuine bootstrap `nominal` rows (separable on `synthetic`).
Because `_regime_instability` is bare string equality over `regime_history`,
each coerced row manufactured spurious transitions, feeding §2's loop.

Repair: PR #1650 (coercion made visible; raw value preserved at
`state_json.regime_raw`) and PR #1653 (migration 063: `TRANSITION` admitted,
coercion sink moved to an explicit `unknown`). Pre-063 casualties are
unrecoverable — the original strings were destroyed before the INSERT — and the
era stays identifiable rather than backfilled.

## 4. What survives as an escape path, and why it is not enough for v7

`trajectory_drift` is genuinely EISV-blind: `TrajectorySignature`
(src/trajectory_identity.py) is built from preferences, beliefs, attractor,
recovery, relational, homeostatic — no E/I/S/V field, no regime, no risk. It is
the one intervention event outside the loop.

It is also ~1,921 events concentrated in **4 agents**, and the signature family
it derives from is separately known to decay into a ~0.633 age attractor
(trajectory-identity maths audit; same finding that confounded the
trajectory-identity paper's §6.5 pilot). Escaping circularity does not confer
statistical power. This is the v7.1/v8 empirical seed, not a v7 section.

The repo has already named the general requirement as **Invariant 4** in
`eisv-stage0-bridge-b-label-routing.md`: every anchor exogenous, never the loop
validating its own trajectory. The v7 question as registered violates it.

## 5. The v7 framing

This lands inside the frame v7 already committed to. `v7-fhat-spec.md` §0
defines F̂ as *the governance observer's surprise over the agent*. An observer
whose state estimate S takes its own categorical output (regime) as a mixture
component, and whose intervention stream is downstream of that same estimate,
has an **undeclared self-loop in its generative model**. That is a
mechanism-level, falsifiable claim about observer design, with a configured
mixture weight, a measured artifact rate (31%), and a named repair. The
registered regression remains unidentified; the mechanism finding does not
convert that design failure into a negative empirical result.

Stated in the paper as: *here is a self-referential path in the observer, a
measured storage artifact that excited it, and the design invariant that
excludes self-validation*. State separately that the registered association was
not identified by its proposed design. Do not call 0.35 a measured coefficient
or effect size, and do not call the unidentified association a structural
negative result.

This also answers `paper-positioning.md`'s recorded worry that pivoting v7 to
ontology "could be read as scope expansion to hide the v7 empirical blocker."
Publishing the blocker's mechanism is the one version of v7 that cannot be
read that way.

## 6. What this note does NOT claim

- Not "EISV is refuted." The finding is about the *validation question's*
  structure, not about EISV's proprioceptive value. The deployed-vs-target
  framing stands.
- Not a negative empirical association. The proposed regression was not run and
  could not serve as external validation; that is `UNIDENTIFIED`, not
  `REFUTED`.
- Not a reopening of outcome-grounding. The pre-registered 2026-12-01 stop-rule
  read (#1425) governs a different question (AUC vs previous-outcome baseline)
  and is untouched by this. No ad-hoc probe reruns.
- Not "the corpus unblocked v7." More endogenous volume does not repair the
  identification failure; the registered association remains `UNIDENTIFIED`.
- Not a fleet-health claim. The 31% artifact is a measurement statement about
  one column's history, repaired going forward by 063.
