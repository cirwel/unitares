# 5 · Reading the signals

[← Integrating agents](04-integrating-agents.md) · [Manual index](README.md) · [Next: Operating →](06-operating.md)

This chapter is the interpretation guide: what the numbers mean, how they're produced, and how much to trust them. The exact formulas with source references are in [`../EISV_COMPUTATION.md`](../EISV_COMPUTATION.md); this is the readable companion.

## 5.1 The pipeline at a glance

```
observables ──► observation blend ──► EMA state ──► residual / basin risk ──► policy action
(decisions,     (behavioral_         (behavioral_    (behavioral_          proceed/
 calibration,    sensor.py)           state.py)       assessment.py)        guide/
 drift, tools)                                                               pause/reject
```

The **behavioral EISV** path (EMA observations from grounded signals) is primary and drives verdicts. The **ODE / thermodynamic model** (`governance_core/`) runs in parallel as a research lens and does **not** drive verdicts by default.

After ~30 check-ins the system builds per-agent **Welford baselines** and assesses by z-score deviation from the agent's *own* operating point, rather than universal thresholds. Before that ("warmup"), the behavioral track scores against fixed universal thresholds while the live verdict falls back to a mostly server-derived cold-start prior (see [EISV_COMPUTATION.md](../EISV_COMPUTATION.md)). Read the result as proprioceptive residuals — state change against a grounded reference — not as an outcome judgment.

## 5.2 The four dimensions

| Coord | What it reads | Deployed computation (heuristic) |
|---|---|---|
| **E** · Energy | Is work advancing? | weighted blend of recent decision-success, coherence, complexity-calibration (and outcomes when present) |
| **I** · Integrity | Do claims match results? | calibration accuracy + coherence trend (+ outcome consistency) |
| **S** · Entropy | Drifting from own normal? | drift-norm + regime instability + complexity divergence |
| **V** · Valence | Energy-vs-integrity imbalance | EMA-smoothed `E − I` (derived) |

Three things to internalize:

1. **Each score is self-relative after warmup.** A "low" number is low *for this agent*, not against a global scale. An ultra-stable agent and a noisy one are graded against different baselines.
2. **V is a readout, not a fifth fact.** It's `E − I` smoothed. Its **sign** is the actionable part: `V > 0` = running hot (energetic, claims outrun results); `V < 0` = running careful (coherent but low progress).
3. **The weights are hand-set, not learned.** That's the honest status of the current estimator — see §5.6.

## 5.3 Verdict and margin

The assessment emits an internal `safe` / `caution` / `high-risk` label, which becomes the action the agent reads back:

| Verdict | Internal sense | Agent action | Recovery path |
|---|---|---|---|
| `proceed` | safe | Continue | — |
| `guide` | slightly off | Read guidance, adjust | — |
| `pause` | needs attention | Stop, reflect | self-recovery or dialectic ([§5.5](#55-recovery-self-recovery--dialectic)) |
| `reject` | significant concern | Human input or dialectic | dialectic |

Total risk is the **sum of named components** (`low_E`, `low_I`, high-`S`, `|V|`, …), each with an explicit weight — no sigmoid/phi black box, so you can trace exactly why a verdict fired ([`src/behavioral_assessment.py`](../../src/behavioral_assessment.py)). **Absolute safety floors always apply**, overriding the self-relative baseline; and self-relative deviation risk is *gated by absolute basin health* — inside the healthy basin, deviation from your own norm is treated as information, not danger.

**Margin** (`comfortable` / `tight` / `critical`) rides along with the verdict, telling you how close the agent is to a basin boundary even while the verdict is still `proceed`.

## 5.4 Calibration: why the signal resists gaming

The system tracks whether stated `confidence` matches **objective** evidence — test pass/fail, command exit codes, lint results — fed back via `record_result` / `outcome_event`. Over time this builds a calibration curve, and persistent overconfidence penalizes **Integrity** through the check-in pipeline.

The practical consequence: an agent can inflate the `confidence` number it reports, but it cannot inflate externally observed evidence. Reported confidence is checked *against results*, so dishonest self-assessment shows up as falling Integrity rather than as a passing grade. Feed verifiable evidence back (`record_result`) to make the calibration meaningful.

## 5.5 Recovery: self-recovery → dialectic

When an agent is paused, recovery is a structured escalation:

1. **Self-recovery** — `self_recovery(reflection=..., mode="quick_resume")` when coherence is high and risk is low; the agent reflects and resumes.
2. **LLM-assisted dialectic** — a local LLM supplies an antithesis for single-agent reflection (no paid API — the operator constraint rules those out).
3. **Peer dialectic** — another agent reviews via `dialectic`: thesis → antithesis → synthesis.

Full protocol: [`../dev/CIRCUIT_BREAKER_DIALECTIC.md`](../dev/CIRCUIT_BREAKER_DIALECTIC.md).

## 5.6 The knowledge graph as a signal source

The KG isn't just storage — searching it *before* acting is how agents avoid re-discovering known failures, and it's where closed mysteries and corrected conclusions live. Discipline (agent-facing): **search before writing**; prefer a linked correction or `supersede` over a fresh note; store something only when a future agent would search for it and not already find it. Operational runbooks belong in `docs/`, not KG notes. Full operating manual: [`../../skills/knowledge-graph/SKILL.md`](../../skills/knowledge-graph/SKILL.md).

## 5.7 Don't trust these numbers blindly

The most important interpretation rule. The deployed EISV is **auditable heuristics over observable behavior**, not the information-theoretic quantities (free energy, mutual information, entropy) the paper targets — those become instrumentable only when the inference layer exposes things like token-level logprobs. Every coordinate carries a provenance tier (`e_source`, `s_source`, …) so a heuristic is never laundered as a measurement.

Whether the numbers add useful signal beyond dumb baselines is an **open, measured question.** The [falsifiability harness](../REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) asks whether EISV/prior-state telemetry adds signal over deliberately boring baselines on ranking (AUC) and calibration (Brier), self-labeling each slice `INCONCLUSIVE` / `SKEPTICAL` / `WEAK SIGNAL` / `KEEP TESTING`. Treat that as a test of calibration and falsifiability for the proprioceptive signal, not the product's headline. Current read: a **weak early signal** at short lead, **no demonstrated prevention**, and a caveat that the lift may be carried by a single `prior_risk` feature rather than the full decomposition. Run it yourself before treating EISV as load-bearing.

For intuition (not a spec), [`../tonality-metaphor.md`](../tonality-metaphor.md) maps key signatures and chromaticism onto EISV, coherence, and drift.

---

[← Integrating agents](04-integrating-agents.md) · [Manual index](README.md) · [Next: Operating →](06-operating.md)
