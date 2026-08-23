# 1 · Overview & concepts

[← Manual index](README.md) · [Next: Installation →](02-install.md)

This chapter gives you the mental model and the vocabulary. Read it once; the rest of the manual assumes these terms.

## 1.1 The problem it solves

Some autonomous-agent failures unfold gradually: loops, repeated tool errors,
miscalibrated confidence, or movement away from prior behavior. Pre-deploy evals
and per-action guardrails answer different questions and may not retain this
longitudinal context.

UNITARES records check-ins **mid-run** and reports state change to the operator
and the agent. Whether a change precedes a bad outcome is measured separately;
the state estimate is not an early-warning guarantee.

## 1.2 Where it fits

UNITARES runs **alongside** your evals and guardrails. It does not replace either.

| Layer | Question it answers | When it acts |
|---|---|---|
| **Evals** | Is this model good enough to ship? | before deploy |
| **Guardrails** | Is this *action* allowed right now? | per action |
| **UNITARES** | What state and evidence does this process have as it works? | continuously, mid-run |

It is **not** an output validator, a sandbox, or a hosted agent platform. It is the runtime *state layer* between evals and guardrails. (Full scope: [`../SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md).)

**Use UNITARES if** you run autonomous coding/research/ops/resident agents, you want mid-run health signals rather than only pre-deploy evals or post-hoc logs, you want agents to check their own state before continuing, and you want an audit trail of confidence, evidence, drift, and recovery.

## 1.3 The core loop

The minimal contract starts a process session, then checks in after meaningful
work:

```python
session = start_session(force_new=True)

result = sync_state(
    response_text=output,
    complexity=0.6,
    confidence=0.8,
    client_session_id=session["client_session_id"],
)
action = result.get("state_summary", {}).get("action")

if action in ("pause", "reject"):
    stop_and_request_review(result)  # implement this in the host client
```

The agent reports **what it did** plus its self-reported `complexity` and `confidence`, and gets back a policy action it can act on using its own state estimate. That's it — no new vocabulary required to *use* it. The vocabulary below is for when you want to act on *why*, not just the action.

## 1.4 The four numbers: EISV

Each check-in returns four proprioceptive scores per agent. With the current
constants, check-ins 1–2 use the Φ cold-start prior, check-ins 3–24 use
behavioral fixed thresholds, and check-in 25 enables self-relative scoring
against the agent's own history (the target reaches full confidence at 30).
Only the last stage is a personalized residual:

| | Name | Reads | Goes wrong when… |
|---|---|---|---|
| **E** | Energy | Is the work advancing? | thrashing, retries, no progress |
| **I** | Integrity | Do claims match results? | high confidence, low actual success |
| **S** | Drift (legacy field: entropy) | Drifting from its reference? | erratic, divergent behavior |
| **V** | Valence | Derived: energy vs integrity | signed imbalance: positive runs hot, negative runs careful |

Two honesty notes that matter for interpretation (full detail in [chapter 5](05-reading-the-signals.md) and [`../EISV_COMPUTATION.md`](../EISV_COMPUTATION.md)):

- **V is derived, not independent.** It is the smoothed `E − I` imbalance. Its *sign* is what's actionable: positive = running hot (energetic but claims outrun results); negative = running careful (coherent but low progress).
- **The deployed numbers are auditable heuristics, not information theory.** The thermodynamic / information-theoretic language (free energy, mutual information, entropy) is the *target* the paper works toward and tests honestly — it is **not** what the running code computes today. The live verdict path is a transparent weighted-threshold model over observable behavior ([`src/behavioral_assessment.py`](../../src/behavioral_assessment.py)).

## 1.5 The verdict

Every check-in resolves to one of four verdicts:

| Verdict | Meaning | What the agent should do |
|---|---|---|
| `proceed` | State is healthy | Continue working normally |
| `guide` | Slightly off track | Read the guidance text and adjust approach |
| `pause` | Needs attention | Stop, reflect, consider a dialectic review |
| `reject` | Significant concern | Open a dialectic review to resolve/contest, or bring in a human |

Verdicts also carry a **margin** (`comfortable` / `tight` / `critical`) indicating proximity to the basin boundary. Definitions live in [`src/governance_glossary.py`](../../src/governance_glossary.py).

## 1.6 Supporting concepts

- **Coherence** — an overloaded compatibility field. Read `coherence_source` and `coherence_role` with it: the default `legacy_tanh_v` value is directional ODE control feedback, while the dormant manifold producer is a structural E/I/S measurement. Neither is an outcome or health score.
- **Ethical drift** — a four-signal vector (calibration deviation, complexity divergence, change in the configured coherence signal, stability deviation) that feeds entropy. The component inherits the coherence producer's semantics; it is not independent outcome evidence.
- **Calibration** — the system tracks whether stated `confidence` matches
  recorded outcomes such as tests, exit codes, or review labels. Evidence from
  CI or an operator is stronger than an agent-authored outcome; if the agent
  controls both channels, calibration can be forged.
- **Knowledge graph (KG)** — a shared discovery store across all agents and sessions, so agents build on each other's findings instead of re-discovering known issues. Agent-facing discipline: [`../../skills/knowledge-graph/SKILL.md`](../../skills/knowledge-graph/SKILL.md).
- **Dialectic** — a structured recovery/review protocol (thesis → antithesis
  → synthesis) that an agent or operator can request after a disputed decision.
- **Identity** — a fresh process mints a fresh agent UUID; cross-process
  continuity is *declared* and assessed, never silently inherited. See
  [chapter 4](04-integrating-agents.md#42-identity-rule) and
  [`../ontology/identity.md`](../ontology/identity.md).

## 1.7 Don't trust the prose — verify it

A central design stance: the project does not ask you to believe the numbers by
prose. On a fresh clone, the
[falsifiability harness](../REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc)
asks whether EISV/prior-state telemetry adds signal over a simple
previous-outcome baseline on AUC and Brier, then compares the selected best
candidate with a matching permutation null. In the frozen 2026-08-09
trusted-anchor matrix, every overall slice is `NON_DETECTION`; none clears the
selection-aware p < 0.05 threshold. There is no demonstrated prevention. That
class is a non-detection rather than a demonstrated absence — the same harness's
first historical power characterisation was withdrawn for corrupted synthetic
pairing, and the corrected hypothetical sensitivity analysis is in the
[power audit](../operations/falsifiability-power-audit-2026-08-23.md). The
frozen cohort's read-specific power is unknown. Run both yourself before relying
on EISV for anything load-bearing.

---

[← Manual index](README.md) · [Next: Installation →](02-install.md)
