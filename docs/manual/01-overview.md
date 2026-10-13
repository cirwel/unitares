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

It is **not** an output validator, a sandbox, or a hosted agent platform. It is
an MCP-native, harness-agnostic runtime *state layer* between evals and
guardrails. MCP is its primary agent-facing contract; it does not own the
agent's reasoning, tool-execution, or user-interaction loop. (Full scope:
[`../SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md).)

The first-party **UNITARES Resident** is intended to supply that agent
experience, but it remains a separate userland built on the same public MCP/SDK
contract as every other harness. Its repository currently ships an early
runtime skeleton, not a usable general-purpose agent. The specialized residents
shipped as examples in this repository are not that product.

**Use UNITARES if** you run autonomous coding/research/ops/resident agents, want runtime state signals, want agents to check their recorded state before continuing, and want an audit trail of confidence, evidence, drift, and recovery.

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
action = result.get("action_summary", {}).get("action")

if action == "pause":  # reject-severity verdicts arrive as pause with sub_action="reject"
    stop_and_request_review(result)  # implement this in the host client
```

The agent reports **what it did** plus its self-reported `complexity` and `confidence`, and gets back a policy action it can act on using its own state estimate. That's it — no new vocabulary required to *use* it. The vocabulary below is for when you want to act on *why*, not just the action.

## 1.4 The four numbers: EISV

Each check-in returns four indices built from runtime observations and controller
history. With the current
constants, check-ins 1–2 use the Φ cold-start prior, check-ins 3–24 use
behavioral fixed thresholds, and check-in 25 enables self-relative risk scoring
against the agent's own history (the target reaches full confidence at 30).
The coordinates remain smoothed indices; assessment adds comparisons with the
agent's baseline alongside absolute thresholds. With behavioral-sensor inputs,
the indices contain these blends:

| | Historical name | What the index contains |
|---|---|---|
| **E** | Energy | Prior decision scores, controller coherence level and complexity divergence, with available outcome, continuity and tool features |
| **I** | Integrity | Calibration error and controller coherence trend, with available outcome-score consistency, continuity and tool-variety features |
| **S** | Entropy | Drift, regime transitions and complexity divergence, with available continuity and tool features |
| **V** | Valence | A separately smoothed raw `E_obs − I_obs` gap |

Physical sensor inputs can also enter the behavioral smoothing stage. Inspect
the input provenance: `primary_eisv_source=behavioral` identifies that output
path, not necessarily a behavioral-sensor input.

V has its own smoothing rate, so it carries temporal information that generally
cannot be recovered by subtracting current E and I. Its sign records the
direction of the smoothed observation gap. Prior decisions also feed back into
E: a policy response can change later readings without a change in task inputs.
These are inspectable features of the instrument, not independent confirmation
of its judgments.

The live policy combines behavioral assessment with ODE structural checks for
basin, void and coherence. The historical names carry intended interpretations;
the implemented indices are defined by their inputs and transformations. See
[chapter 5](05-reading-the-signals.md) and
[`../EISV_COMPUTATION.md`](../EISV_COMPUTATION.md) for formulas, provenance and a
worked example.

## 1.5 The verdict

The decision engine returns `proceed` or `pause`, qualified by a sub-action.
Responses expose `guide` as a policy action; reject-severity decisions use
`action="pause"` with `sub_action="reject"`:

| Verdict | Meaning | What the agent should do |
|---|---|---|
| `proceed` | Policy permits continuation | Continue working normally |
| `guide` | Continuation with guidance | Read the guidance text and adjust approach |
| `pause` | A hard stop: check-ins and new shared-memory entries are refused, not queued | Stop and read the reason; `self_recovery(action="check")` reports self-recovery eligibility |
| `reject` | Significant concern | Open a dialectic review to resolve/contest, or bring in a human |

Verdicts also carry a **margin** (`settling` / `comfortable` / `tight` / `warning` / `critical`) indicating proximity to the nearest measurable *decision threshold* — risk, void, or coherence — which is a different notion of edge from basin membership: an agent inside the `boundary` basin can still report `comfortable`. Verdict definitions live in [`src/governance_glossary.py`](../../src/governance_glossary.py); the margin levels come from `GovernanceConfig.compute_proprioceptive_margin`.

## 1.6 Supporting concepts

- **Coherence** — an overloaded compatibility field. Read `coherence_source` and `coherence_role` with it: the default `legacy_tanh_v` value is directional ODE control feedback, while the dormant manifold producer is a structural E/I/S measurement. Neither is an outcome or health score.
- **Ethical drift** — a four-signal vector (calibration deviation, complexity divergence, change in the configured coherence signal, stability deviation) that feeds entropy. The component inherits the coherence producer's semantics; it is not independent outcome evidence.
- **Calibration** — the system tracks whether stated `confidence` matches
  recorded outcomes such as tests, exit codes, or review labels. Evidence from
  CI or an operator is stronger than an agent-authored outcome; if the agent
  controls both channels, calibration can be forged. The calibration input
  currently blended into I on the handler path is a lifetime fleet aggregate of
  strategic confidence-versus-trajectory-health bins, not a current-claim check.
- **Knowledge graph (KG)** — a shared discovery store across all agents and sessions, so agents build on each other's findings instead of re-discovering known issues. Agent-facing discipline: [`../../skills/knowledge-graph/SKILL.md`](../../skills/knowledge-graph/SKILL.md).
- **Dialectic** — a structured recovery/review protocol (thesis → antithesis
  → synthesis) that an agent or operator can request after a disputed decision.
- **Identity** — a fresh process mints a fresh agent UUID; cross-process
  continuity is *declared* and assessed, never silently inherited. See
  [chapter 4](04-integrating-agents.md#42-identity-rule) and
  [`../ontology/identity.md`](../ontology/identity.md).

## 1.7 Inspect the signal; evaluate its use

The [computation reference](../EISV_COMPUTATION.md) defines what each reading
records. A narrow or derived signal remains information to inspect. Whether a
particular use improves diagnosis, prediction or decisions is a separate
question, tracked in the [claim ledger](../EVIDENCE_AND_LIMITS.md#current-claim-status).

For outcome prediction, inspect the
[frozen 2026-08-09 matrix](../operations/eisv-ablation-frozen-2026-08-09.md)
and its [power audit](../operations/falsifiability-power-audit-2026-08-23.md).
That frozen matrix records `NON_DETECTION` under its tested support and design;
it does not establish that the instrument has no signal or cannot be improved.
The [reviewer guide](../REVIEWER_GUIDE.md#falsifiability-inspect-the-registered-evidence)
provides synthetic controls that run on a fresh clone. Deployment outcome
discrimination remains reserved for the registered execution under the
[stop rule](../proposals/registered/eisv-outcome-grounding-stop-rule-v0.md).

---

[← Manual index](README.md) · [Next: Installation →](02-install.md)
