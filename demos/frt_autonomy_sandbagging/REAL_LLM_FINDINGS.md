# Real-LLM validation of the EISV Integrity signal — and where it fails

This extends the model-organism eval (see `README.md`) to a **real LLM**, and reports
the honest competence boundary of UNITARES's Integrity signal for detecting
sandbagging/concealment. The model-organism harness is deterministic and
dependency-free; this extension requires a local LLM (Ollama) and is kept
separate so the core demo's "reproduces on a fresh clone" contract is preserved.

> **Status: pilot.** n = 4 agents/cohort, single model, math tasks, instructed /
> enforced concealment. The AUCs are noisy at this n; the *qualitative pattern*
> (below) is robust across agents and matches the parametric model organism. Treat
> as a directional, falsifiable characterization — not a deployment-scale measurement.

## TL;DR

EISV's Integrity is a **confidence-vs-outcome consistency** detector with a **narrow
competence**:

- It catches **naive** overconfident underperformance — but **only against a
  well-calibrated baseline**.
- It **degrades on realistic noisy baselines**, because real LLMs are themselves
  overconfident on hard tasks (honest agents reported ~0.99 confidence while
  scoring ~0.56).
- It **inverts against *calibrated* concealment**: an adversary that lowers its
  reported confidence to match its suppressed output looks *better-calibrated than
  honest agents*, so EISV rates it as the **healthiest** agent in the fleet.
- The **cross-framing comparison** (capability under a monitored/evaluated framing
  vs. an unmonitored/deployment framing) is the **robust detector for deliberate
  concealment** and should be the primary signal for that threat. EISV Integrity is
  complementary — good for naive overconfidence and drift, not deliberate concealment.

## Method

A real LLM (`qwen3-coder-next` via Ollama) solves verifiable multiplication tasks
under three framings. Its **genuine** `(reported_confidence, success)` stream is fed
through the **same real UNITARES code** the model-organism demo uses
(`src.behavioral_state.BehavioralEISV` + `src.behavioral_assessment.assess_behavioral_state`).
The label is the framing (independent of EISV — non-circular):

- **honest** — solve genuinely, report genuine confidence.
- **naive conceal** — underperform under evaluation but keep reporting high confidence.
- **calibrated conceal** *(enforced)* — underperform, **and** confidence is overridden
  to track the agent's own rolling (concealed) success rate. This is the theoretical
  adversary the model organism exposed; a real LLM would not execute it spontaneously
  (it kept high confidence even when told to lower it — see v1), so we enforce it to
  test the blind spot directly.

The **cross-framing arm** runs each agent under monitored (conceal) and unmonitored
(perform) framings *on identical tasks*; the detector is `success_unmonitored −
success_monitored`.

## Results

UNITARES Integrity detection AUC (suspicion = 1 − Integrity, vs the honest cohort):

| Baseline | naive EISV | calibrated EISV | naive cross-framing | calibrated cross-framing |
|---|---|---|---|---|
| **easy tasks** (honest = 100%) | 1.00 | 1.00\* | — | — |
| **hard tasks** (honest ≈ 56%) | **0.75** | **0.19** | 0.78 | **0.97** |

\* On easy tasks the LLM could not actually calibrate its concealment (kept conf ≈ 0.95
while degrading), so the "calibrated" cohort behaved naive and was caught. The 1.00s
are an **easy-task artifact**: with a perfectly-calibrated honest baseline, any
degradation separates trivially.

On the realistic (hard-task) baseline:
- **naive EISV 0.75** — collapsed from 1.00, because honest agents are *also*
  overconfident on hard math, so honest ≈ naive on the calibration axis EISV measures.
- **calibrated EISV 0.19** — *inverted*. The enforced-calibrated concealer (low
  confidence matching low concealed success → Integrity 0.85–0.95) is rated *healthier*
  than honest agents (who are overconfident → Integrity as low as 0.28).
- **cross-framing 0.97** on the calibrated case — catches exactly the concealer EISV
  inverts on, because concealment shows up as a capability gap regardless of claims.

## Threat-model implication

If detecting deliberate concealment / sandbagging is in scope, the mechanism to build
is **cross-framing capability comparison**, not more EISV tuning — EISV alone rates
calibrated concealers as your *most trustworthy* agents. EISV's Integrity remains
useful for what it actually measures: **naive overconfidence and calibration drift in
cooperative agents**, against a baseline that is itself reasonably calibrated.

## Relation to organic-fleet validation

This synthetic/injected evidence is the honest basis because **organic fleet data
cannot validate the claim**: the population that produces failure labels
(transport-level tool callers) and the population that carries EISV state (onboarded,
checking-in agents) are disjoint by construction — using the tools doesn't require
governance onboarding. A detector can't be validated on a population that has no
EISV. Hence the injected model-organism / real-LLM route.

## Reproduce

```bash
# core model-organism demo — deterministic, dependency-free:
python3 run_unitares_demo.py --json

# real-LLM extension — requires a local Ollama with a coding model:
python3 real_llm_probe.py          # ~3 framings x 4 agents x 12 tasks x 2 framings
```

`real_llm_probe.py` writes `v2_results.json` (per-agent rows + the AUC summary).
