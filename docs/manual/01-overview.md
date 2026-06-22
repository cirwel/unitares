# 1 · Overview & concepts

[← Manual index](README.md) · [Next: Installation →](02-install.md)

This chapter gives you the mental model and the vocabulary. Read it once; the rest of the manual assumes these terms.

## 1.1 The problem it solves

Autonomous and semi-autonomous agents fail *gradually*. By the time output visibly breaks, the agent has usually been drifting for a while — looping, getting overconfident, wandering off its own normal behavior. Pre-deploy evals can't see this (they run before the work), and per-action guardrails can't either (they only see one action at a time).

UNITARES watches each agent **mid-run** and reports degradation as it happens — to you *and to the agent itself* — while it's still just numbers moving and not yet broken output.

## 1.2 Where it fits

UNITARES runs **alongside** your evals and guardrails. It does not replace either.

| Layer | Question it answers | When it acts |
|---|---|---|
| **Evals** | Is this model good enough to ship? | before deploy |
| **Guardrails** | Is this *action* allowed right now? | per action |
| **UNITARES** | Is this agent *still healthy* as it works? | continuously, mid-run |

It is **not** an output validator, a sandbox, or a hosted agent platform. It is the runtime *state layer* between evals and guardrails. (Full scope: [`../SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md).)

**Use UNITARES if** you run autonomous coding/research/ops/resident agents, you want mid-run health signals rather than only pre-deploy evals or post-hoc logs, you want agents to check their own state before continuing, and you want an audit trail of confidence, evidence, drift, and recovery.

## 1.3 The core loop

The entire contract is two calls inside the agent's loop:

```python
# After a unit of work:
result = sync_state(response_text=output, complexity=0.6, confidence=0.8)
verdict = result.get("verdict", {}).get("value")   # proceed / guide / pause / reject

if verdict in ("pause", "reject"):
    agent.require_human_review(result["verdict"]["next_action"])
```

The agent reports **what it did** plus its self-reported `complexity` and `confidence`, and gets back a verdict it can act on *before* an external guardrail has to fire. That's it — no new vocabulary required to *use* it. The vocabulary below is for when you want to act on *why*, not just the verdict.

## 1.4 The four numbers: EISV

Each check-in returns four scores per agent, each graded against that agent's **own** ~30-check-in baseline (so slow drift surfaces even while output still looks fine):

| | Name | Reads | Goes wrong when… |
|---|---|---|---|
| **E** | Energy | Is the work advancing? | thrashing, retries, no progress |
| **I** | Integrity | Do claims match results? | high confidence, low actual success |
| **S** | Entropy | Drifting from its own normal? | erratic, divergent behavior |
| **V** | Valence | Derived: energy vs integrity | motion without coherence (or vice-versa) |

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

- **Coherence** — a scalar summary of how internally consistent the agent's state is; it modulates risk and is one input to the E/I observations.
- **Ethical drift** — a four-signal vector (calibration deviation, complexity divergence, coherence deviation, stability deviation) that feeds entropy. No human oracle is needed for runtime drift estimation.
- **Calibration** — the system tracks whether stated `confidence` matches *real* outcomes (test pass/fail, exit codes, lint). Persistent overconfidence costs Integrity. This is why an agent can inflate its `confidence` number but not its success rate.
- **Knowledge graph (KG)** — a shared discovery store across all agents and sessions, so agents build on each other's findings instead of re-discovering known issues. Agent-facing discipline: [`../../skills/knowledge-graph/SKILL.md`](../../skills/knowledge-graph/SKILL.md).
- **Dialectic** — the structured recovery/peer-review protocol (thesis → antithesis → synthesis) an agent enters after a `pause`/`reject`.
- **Identity** — a fresh process mints a fresh agent UUID; cross-process continuity is *declared* and verified, never silently inherited. See [chapter 4](04-integrating-agents.md#43-identity-the-one-rule-that-matters) and [`../ontology/identity.md`](../ontology/identity.md).

## 1.7 Don't trust the prose — verify it

A central design stance: the project does not ask you to believe the numbers predict anything. On a fresh clone, the [falsifiability harness](../REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) scores EISV against a deliberately dumb baseline (AUC, Brier) and self-labels each slice `INCONCLUSIVE` / `SKEPTICAL` / `WEAK SIGNAL` / `KEEP TESTING` rather than asserting. The current honest read is a *weak early signal* at short lead, with no demonstrated prevention — run it yourself before relying on it for anything load-bearing.

---

[← Manual index](README.md) · [Next: Installation →](02-install.md)
