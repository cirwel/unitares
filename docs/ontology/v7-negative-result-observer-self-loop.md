# v7 empirical section: the observer's undeclared self-loop

**Status:** decided direction for the v7 empirical section, 2026-08-13.
Every claim below was verified adversarially — the refutation was attempted
against source at `origin/master` and against the full live audit record, not
sampled — before the direction was set. Supersedes "blocked on audit-corpus
maturity" as the description of where v7's empirics stand.

**One sentence:** the registered v7 question — *do agents whose basin
assignment flips also receive governance interventions?* — is unanswerable on
this system not for lack of data but because the regressor is an arithmetic
summand of the regressand, with a known coefficient; v7 reports that mechanism,
with its coefficient and its measured artifact rate, as the empirical finding.

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

## 2. The correlation is circular by arithmetic, not merely by provenance

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

X is a weighted summand of the pipeline that produces Y. A positive
correlation is not evidence; a portion of the effect size is computable a
priori from the 0.40/0.35/0.25 weights.

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
whose state estimate S takes its own categorical output (regime) as a 0.35
summand, and whose intervention stream is downstream of that same estimate, has
an **undeclared self-loop in its generative model**. That is a mechanism-level,
falsifiable claim about observer design — with a measured coefficient (0.35), a
measured artifact rate (31%), and a named repair — not a lament that data was
insufficient.

Stated in the paper as: *here is a self-referential loop in the observer, its
coefficient, its artifact rate, and the design invariant that excludes it* —
never as *we could not measure*.

This also answers `paper-positioning.md`'s recorded worry that pivoting v7 to
ontology "could be read as scope expansion to hide the v7 empirical blocker."
Publishing the blocker's mechanism is the one version of v7 that cannot be
read that way.

## 6. What this note does NOT claim

- Not "EISV is refuted." The finding is about the *validation question's*
  structure, not about EISV's proprioceptive value. The deployed-vs-target
  framing stands.
- Not a reopening of outcome-grounding. The pre-registered 2026-12-01 stop-rule
  read (#1425) governs a different question (AUC vs previous-outcome baseline)
  and is untouched by this. No ad-hoc probe reruns.
- Not "the corpus unblocked v7." Volume cleared; the question died anyway. The
  blocker was never sample size.
- Not a fleet-health claim. The 31% artifact is a measurement statement about
  one column's history, repaired going forward by 063.
