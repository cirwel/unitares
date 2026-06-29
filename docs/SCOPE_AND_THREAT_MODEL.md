# Scope, threat model, and why the signal is trustworthy

This is the deeper justification that used to live in the repo README. It answers
three questions a careful evaluator asks: *who is this for*, *why can't an agent
just game it*, and *what is it honestly not robust against yet*.

For how the numbers are actually computed, see [How EISV is computed](EISV_COMPUTATION.md);
for the cold-evaluator path and the falsifiability harness, see the
[Reviewer Guide](REVIEWER_GUIDE.md).

## Who should integrate this

UNITARES is for you if you run **multiple long-lived autonomous agents** —
tool-using, multi-step, doing real work over hours or days — and you've watched
an agent quietly drift without anyone noticing until something visible broke. The
check-in loop surfaces that drift while it's still just numbers moving (Integrity
slipping, overconfidence climbing) instead of waiting for a user to complain.

**The threshold that matters is check-in count, not wall-clock time.**
Self-relative grading needs roughly **30 check-ins** to establish an agent's
baseline (absolute safety floors apply before that). An agent doing dozens of
units of work — over an hour or a week — crosses it; one that does three and
exits never does. That's the real line for "is my session long enough to
benefit," not a duration.

**Probably not worth it yet for** short-lived chatbot turns, where per-turn
overhead outweighs the benefit, or for teams that can't instrument their agent
loop.

## Why an agent can't just inflate its own confidence

Self-reported confidence is only one input. UNITARES also watches **real outcomes
it can verify** — test pass/fail, exit codes, tool results — sent back via
`record_result()`. Over many tasks it compares the agent's *claimed* confidence
against its *actual* success rate. An agent that reports `confidence=0.9` while
only succeeding 50% of the time builds up a track record of being overconfident;
its Integrity (I) drops, and the verdict shifts to `guide` or `pause`. The signal
is anchored to what actually happened, not to what the agent said about itself.

After about 30 check-ins, the four numbers are graded against the agent's *own*
running history rather than a one-size-fits-all threshold. Absolute safety floors
still apply on top of that.

State lives in PostgreSQL + AGE. **The verdict path is the auditable behavioral
model** — component risk plus self-relative z-scores, source in
[`src/behavioral_assessment.py`](../src/behavioral_assessment.py). A separate
dynamical-systems model (`governance_core/`, the thermodynamic / free-energy
formulation) runs **in parallel as a research cross-check and does not drive
verdicts by default** ([`governance_monitor.py`](../src/governance_monitor.py):
*"the ODE runs in parallel but does NOT drive verdicts… primary verdicts come
from behavioral assessment"*). Its derivation is in
[Paper v6](https://github.com/cirwel/unitares-paper-v6) (DOI 10.5281/zenodo.19647159).

## What makes the signal trustworthy

**Trajectory as identity.** An agent's EISV pattern over time answers continuity
questions ("is this still the same agent across restarts?") and surfaces slow
drift that no single check-in could catch.

**Peer review when it matters.** When an agent's confidence and the system's
assessment disagree, UNITARES runs a short back-and-forth review with other
agents — or with an LLM when no peers are around — before anything halts. (In the
codebase and papers this is called *dialectic*.) See
[dialectic-dataset](https://github.com/cirwel/dialectic-dataset).

## Scope and threat model

UNITARES is **adversarial-aware, not adversarial-naive**, with deliberately
conservative enforcement. One channel is anchored to outcomes an agent can't
fake: the Integrity (I) calibration term is trained only on *hard-exogenous*
outcomes — test pass/fail, exit codes, task results above an evidence-weight
floor (`record_result()` → `outcome_events.py`, `HARD_EXOGENOUS_TYPES`);
self-declared synthetic outcomes are excluded from the calibration channels by
construction. So over a track record an agent can inflate its self-reported
`confidence` but not its actual success rate, and drift (S) is graded against its
*own* prior trajectory, so there is no fixed threshold to pre-game. **That
outcome-anchoring is real but partial — it is one input, not the whole verdict,
and at current maturity it is not the dominant one. Read _What actually drives
the verdict today_ below before treating the anchoring as a robustness
guarantee.** Verdicts lean toward `proceed`/`guide` over `pause`/`reject` by
design — a false-positive pause on a productive agent is itself a failure mode
(acutely so here, since the agents building UNITARES run under it). And "no ethics
classifier" means no hand-labeled ethics model, *not* that the system is
value-free: drift (S) is a salience flag, not a verdict.

### What actually drives the verdict today

The outcome-anchoring above is genuine but easy to over-read. Two scoping facts,
both disclosed in-band on every `process_agent_update` (the `risk_attribution`
block) and demonstrated in
[`operations/self-report-verdict-dependence-2026-06-28.md`](operations/self-report-verdict-dependence-2026-06-28.md):

1. **The calibration anchor is dormant until it has evidence.**
   `compute_calibration_error` returns `None` until a confidence bin holds ≥10
   hard-exogenous samples ([`src/monitor_drift.py`](../src/monitor_drift.py)).
   Until then the outcome anchor contributes nothing and the verdict runs on
   self-attested inputs alone.
2. **The enforcement verdict is dominated by self-attested inputs.** At current
   maturity it is set by the caller's self-reported `ethical_drift` (plus
   `complexity` / `confidence`), not by an independent measurement of what the
   agent did. Pre-warmup — behavioral confidence `< 0.3`, i.e. before ~update #3
   ([`src/governance_monitor.py`](../src/governance_monitor.py)) — the behavioral
   channel is telemetry-only and the verdict is a *pure function of self-attested
   inputs*. Post-warmup the behavioral signal folds in **escalate-only**: it takes
   the more-severe verdict and the max risk, so it can *raise* a verdict but
   cannot pull a clean-but-lying Φ down to match dishonest behavior. Demonstrated:
   a benign refactor and a *confessed sabotage* submitted with identical
   `ethical_drift=[0,0,0]` receive indistinguishable `proceed/safe` verdicts — the
   saboteur even scored marginally *lower* risk.

The system does not hide this: every verdict carries
`risk_attribution.primary_driver: "self_reported"`, and the high-risk guidance text
states in plain language that the inputs are self-attested. Closing the gap —
making the φ→behavioral blend drift-robust while preserving the escalate-only
invariant — is v2-adjacent work tracked in
[`proposals/continuous-verdict-blending-v0.md`](proposals/continuous-verdict-blending-v0.md).

### Signal provenance — attested vs observed

| Input | Provenance | Anchored to | Movable in one call? |
|---|---|---|---|
| `confidence` | agent-attested | clamped to `coherence + 0.3` on KG writes | yes, within the clamp |
| `complexity` | agent-attested | server estimates a divergence term against it | the report yes; the divergence no |
| `ethical_drift` | agent-attested | nothing independent at current maturity | yes — **and it dominates the verdict** |
| Integrity calibration term | derived | hard-exogenous outcomes only (tests/tasks, evidence-gated) | no — needs ≥10 samples/bin |
| Coherence (ρ) | derived | server-computed E–I divergence | no |
| Behavioral text-risk | derived | the response text itself | no — escalate-only, unweighted pre-warmup |
| Self-relative baseline | derived | the agent's own EISV history | only slowly, within observed signals |
| Absolute floors (E,I ≥ 0.30; S ≤ 0.70; \|V\| ≤ 0.50) | hardcoded | — | never — cannot be talked out of ([`behavioral_assessment.py`](../src/behavioral_assessment.py)) |

Honest one-line robustness statement: **the Integrity/calibration channel is
genuinely hard to game over a track record; the live verdict is currently only as
trustworthy as the agent's self-reported `ethical_drift` — totally so before the
behavioral baseline warms, and dominantly so after.**

**The genuine open question.** Robustness against a *motivated* attacker
deliberately optimizing the EISV proxy, at scale, is unproven — red-teaming so far
has been ad hoc rather than systematic or sustained, and the deployment is
single-operator. That is the real limitation: a shortfall of *sustained
adversarial testing*, not of adversarial design.

**A named, demonstrated blind spot: calibrated capability concealment.** The
Integrity (I) signal anchors confidence to outcomes, so it catches an agent that
*over*-claims — including a sandbagger that suppresses its performance under
evaluation while still reporting high confidence. It does **not** catch an agent
that suppresses performance *and* lowers its reported confidence to match: such an
agent stays calibrated and is indistinguishable from an honestly less-capable one.
This is structural, not a tuning gap — every EISV dimension is computed in-band,
from what the agent does and claims inside the monitored session, so concealment
that leaves the agent's claims internally consistent leaves no in-band trace. The
instrument that catches it is out-of-band — comparing performance across a
monitored vs. an unmonitored framing — which is a capability-elicitation probe, a
different tool than this runtime drift signal. A reproducible model-organism
demonstration of both the catch (naive sandbagging, detection AUC ≈ 1.0) and the
blind spot (calibrated sandbagging, AUC ≈ 0.5 against the real `BehavioralEISV` +
`assess_behavioral_state` path) was built as the `frt_autonomy_sandbagging`
model-organism demo, since removed from the repo as out-of-scope (see
[`REPO_SCOPE.md`](REPO_SCOPE.md)); its findings are recorded here.
A real-LLM extension (`real_llm_probe.py`) sharpened the picture against a *noisy*
honest baseline — real models are themselves overconfident on hard tasks, so the
naive catch degrades to AUC ≈ 0.75 and the calibrated blind spot **inverts** to
≈ 0.19 (Integrity rates a calibrated concealer as *healthier* than honest agents),
while the out-of-band cross-framing probe still catches it (≈ 0.97). In short: the
Integrity signal is a calibration-consistency detector for naive overconfidence and
drift, **not** a deliberate-concealment detector — for that, use cross-framing.
Scope note: that demonstration exercises the behavioral-EISV core and verdict, not
the full server pipeline (sensors, continuity, dialectic peer review); the
structural argument is expected to carry to those paths but has not been measured
there.

