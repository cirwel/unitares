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

**Before the behavioral baseline warms** (behavioral confidence < 0.3 — the first
few check-ins), the verdict falls back to the Φ cold-start prior: computed mostly
from server-derived signals (complexity divergence, coherence, calibration), with
any self-reported `ethical_drift` only a capped ≤30% blend and the independent
behavioral signal telemetry-only. Described-behavior semantics don't fully register
until the agent is warm; the self-relative defenses described here are the warmed
steady state, not the cold-start regime.

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
conservative enforcement. The signal is anchored to outcomes an agent can't fake
— test pass/fail, exit codes, tool results via `record_result()`: an agent can
inflate its self-reported `confidence` but not its actual success rate, and drift
(S) is graded against its *own* prior trajectory, so there is no fixed threshold
to pre-game. Verdicts lean toward `proceed`/`guide` over `pause`/`reject` by
design — a false-positive pause on a productive agent is itself a failure mode
(acutely so here, since the agents building UNITARES run under it). And "no ethics
classifier" means no hand-labeled ethics model, *not* that the system is
value-free: drift (S) is a salience flag, not a verdict, and Integrity (I) is
anchored to ground-truth outcomes rather than to the agent's own history.

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

