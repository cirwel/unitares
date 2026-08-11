# Scope, threat model, and signal limits

This is the deeper justification that used to live in the repo README. It answers
three questions a careful evaluator asks: *who is this for*, *what anchors the
signal*, and *what is it not robust against yet*.

For how the numbers are actually computed, see [How EISV is computed](EISV_COMPUTATION.md);
for the cold-evaluator path and the falsifiability harness, see the
[Reviewer Guide](REVIEWER_GUIDE.md).

## Who should integrate this

UNITARES is for you if you run **multiple long-lived autonomous agents** —
tool-using, multi-step, doing real work over hours or days — and you've watched
an agent quietly drift without anyone noticing until something visible broke. The
check-in loop makes mid-run state changes inspectable. Whether those changes
predict a later incident or support effective prevention is an open evaluation
question, not a premise of the integration.

**The threshold that matters is check-in count, not wall-clock time.**
With the current constants, the cold-start prior owns check-ins 1–2, behavioral
fixed-threshold scoring owns check-ins 3–24, and self-relative scoring starts at
check-in 25 against a 30-update baseline target. Absolute safety floors apply in
every stage. An agent doing dozens of
units of work — over an hour or a week — crosses it; one that does three and
exits never does. That's the real line for "is my session long enough to
benefit," not a duration.

**Probably not worth it yet for** short-lived chatbot turns, where per-turn
overhead outweighs the benefit, or for teams that can't instrument their agent
loop.

## What grounds confidence — and where that boundary fails

Self-reported confidence is only one input. UNITARES can also consume outcomes
such as test pass/fail, exit codes, tool results, CI labels, or operator review
through `record_result()`. When those records come from a trusted client or
external integration, they give calibration a stronger anchor than the agent's
own narrative. Persistent high confidence paired with poor recorded outcomes can
lower Integrity (I) and raise risk.

The generic API does not make an agent-authored outcome cryptographically true.
If the monitored process controls both its confidence and every outcome record,
it can forge a consistent story. Outcome provenance and integration boundaries
therefore matter; CI- or operator-authored evidence is stronger than an
unverified self-report.

From check-in 25 with the current constants, the four numbers are graded against
the agent's *own* running history rather than only a one-size-fits-all threshold.
Absolute safety floors still apply on top of that.

**Before the behavioral channel reaches confidence 0.3** (check-ins 1–2), the
verdict comes from the Φ cold-start prior: computed mostly
from server-derived signals (complexity divergence, coherence, calibration), with
any self-reported `ethical_drift` only a capped ≤30% blend and the independent
behavioral signal telemetry-only. Described-behavior semantics don't fully register
until check-in 3; self-relative scoring still waits until check-in 25.

State lives in PostgreSQL + AGE. **The verdict path is the auditable behavioral
model** — component risk plus self-relative z-scores, source in
[`src/behavioral_assessment.py`](../src/behavioral_assessment.py). A separate
dynamical-systems model (`governance_core/`, the thermodynamic / free-energy
formulation) runs **in parallel as a research cross-check and does not drive
verdicts by default** ([`governance_monitor.py`](../src/governance_monitor.py):
*"the ODE runs in parallel but does NOT drive verdicts… primary verdicts come
from behavioral assessment"*). Its derivation is in
[Paper v6](https://github.com/cirwel/unitares-paper-v6) (DOI 10.5281/zenodo.19647159).

## What anchors the signal

**Trajectory as continuity evidence.** An EISV pattern can contribute to the
question "does this process behave like the recorded predecessor?" and can
surface slow drift that no single check-in shows. It is a forgeable behavioral
similarity signal, not authentication or proof of personal identity.

**Structured review when requested.** After a pause or disputed decision, an
agent or operator can open a thesis–antithesis–synthesis review with another
agent or a configured LLM reviewer. Review is an available recovery path, not a
guarantee that an independent reviewer ran before the original pause. (In the
codebase and papers this is called *dialectic*.) See
[dialectic-dataset](https://github.com/cirwel/dialectic-dataset).

## Scope and threat model

UNITARES exposes several adversarial boundaries rather than claiming to close
them. Trusted external outcomes are harder for a monitored agent to forge than
self-report, but an agent-controlled `record_result()` call is not independent
evidence. Self-relative scoring removes one universal cut after check-in 25, but
a patient actor may still shape or poison its own baseline. Verdicts lean toward
`proceed`/`guide` over `pause`/`reject` by design because false-positive pauses
are also costly. Drift (S) is a salience signal, not an ethics label; Integrity
(I) is only as grounded as the outcome channel feeding it.

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
This is structural, not merely a tuning gap — every EISV dimension is computed in-band,
from what the agent does and claims inside the monitored session, so concealment
that leaves the agent's claims internally consistent leaves no in-band trace. The
instrument that catches it is out-of-band — comparing performance across a
monitored vs. an unmonitored framing — which is a capability-elicitation probe, a
different tool than this runtime drift signal. An internal model-organism probe
previously exercised this failure shape, but its implementation was removed as
out of repository scope (see [`REPO_SCOPE.md`](REPO_SCOPE.md)); it is therefore
not reproducible evidence for an external evaluator. The load-bearing claim here
is narrower: calibration consistency cannot distinguish honest low capability
from capability that is concealed while confidence is reduced to match. Test
deliberate concealment with an independent, cross-framing capability evaluation.

**The highest-stakes surface: the governed-effect execute plane.** Everything above
concerns the *signal* and whether an agent can game it. The most security-relevant
capability is different in kind: an optional **governed-effect execute plane** (BEAM,
port 8788) where an agent proposes an effect — `agent_spawn`, `file_write`, or a
file-write commit — and only governance commits it. That is a genuine new safety
property ("agents propose, governance commits"), but it is also a host code-execution
surface, so it is the place a motivated attacker would aim. It is **off by default**
(all three execute flags default off; the shipped plist templates do not enable them;
a fail-closed boot guard blocks commit-without-dispatch), and when an operator enables
it every effect is gated by strong-tier identity re-certification, a per-effect
governance veto (`POST /v1/effect-veto`) on every path, a bearer token, loopback
binding, per-class payload ceilings, and content-hash reversibility. The honest
residual: those gates have had the same ad-hoc-rather-than-sustained adversarial
testing as the rest of the system, and a compromised strong-tier proposer credential
would convert to host execution — so the credential boundary is load-bearing. The
security-reporting contract for this surface lives in [`SECURITY.md`](../SECURITY.md).
