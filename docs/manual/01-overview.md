# 1 · Overview & concepts

[← Manual index](README.md) · [Next: Installation →](02-install.md)

This chapter gives you the mental model and the vocabulary. Read it once; the rest of the manual assumes these terms.

## 1.1 The problem it solves

Long-running agent work outlives the process that started it. Context windows
reset, processes restart, and work passes between agents, runtimes and vendors.
Without a shared record, a successor cannot tell who made a claim, what
supports it, who challenged it or what actually happened, and an operator
cannot reconstruct the work afterwards.

UNITARES keeps that record on a server the operator controls. Each runtime
keeps its own models, tools and execution loop; the record is what they share.

## 1.2 Where it fits

UNITARES runs **alongside** your evals and guardrails. It does not replace either.

| Layer | Question it answers | When it acts |
|---|---|---|
| **Evals** | Is this model good enough to ship? | before deploy |
| **Guardrails** | Is this *action* allowed right now? | per action |
| **UNITARES** | Who did this work, on what evidence, who challenged it, and what happened? | as the work runs, and afterwards |

It is **not** an output validator, a sandbox, or a hosted agent platform. It
does not own the agent's reasoning, tool-execution, or user-interaction loop;
MCP is its primary agent-facing contract. (Full scope:
[`../SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md).)

The first-party **UNITARES Resident** supplies that agent experience as a
separate userland on the same public MCP/SDK contract as every other harness.
Its repository ships a runtime skeleton today; the agent itself has not shipped yet.
The specialized residents in this repository are reference examples.

**Use UNITARES if** you run several long-lived agents (coding, research, ops or
resident), want their work attributable across restarts and handoffs, want
findings, review and outcomes kept with the process that made them, and want
agents to read that record before continuing.

## 1.3 The core loop

A process binds an identity, reads the record before acting, writes what it
found, checks in after meaningful work, and records the outcome:

```python
session = start_session(force_new=True)
csid = session["client_session_id"]

prior = search_shared_memory(query="flaky upload retry test")  # reads need no identity

finding = store_finding(
    client_session_id=csid,
    discovery_type="bug_found",
    summary="Upload retry test races the mock server's shutdown.",
)

result = sync_state(
    client_session_id=csid,
    response_text="Fixed the race and ran the focused tests.",
    complexity=0.4,
    confidence=0.7,
)
if result.get("enforcement", {}).get("applied"):
    handle_applied_pause(result)  # explicit enforce posture; see §1.5
elif result.get("policy_evaluation", {}).get("action") in {"pause", "reject"}:
    inspect_recommendation(result)  # observational by default; continuing is allowed

record_result(
    client_session_id=csid,
    outcome_type="test_passed",
    prediction_id=result.get("prediction_id"),
)
```

A successor process repeats `search_shared_memory` before acting, and declares
lineage only when it is taking over from a finished predecessor. The
[agent session walkthrough](../guides/AGENT_SESSION_WALKTHROUGH.md) captures
both processes with every response.

## 1.4 The record

The shared record answers five questions about any piece of work:

| Question | Mechanism |
|---|---|
| **Who said it?** | Process identity and explicit lineage; lineage grants no authority. |
| **What supports it?** | Durable findings with their corrections and provenance. Each `sync_state` check-in keeps a bounded excerpt of its report text, the agent's own words rather than a verified claim; a durable claim needs a finding. |
| **Who challenged it?** | Structured review that preserves positions, objections, conditions and resolution. |
| **What happened?** | Typed outcome events with their evidence grade, bound to a check-in's prediction when supplied. |
| **What can a successor recover?** | Search, review reads, history export and authorized outcome reads. |

- **Identity** — a fresh process mints a fresh agent UUID; cross-process
  continuity is *declared* and assessed, never silently inherited. See
  [chapter 4](04-integrating-agents.md#42-identity-rule) and
  [`../ontology/identity.md`](../ontology/identity.md).
- **Shared memory (knowledge graph, KG)** — a shared discovery store across all agents and sessions, so agents build on each other's findings instead of re-discovering known issues. Search it with `search_shared_memory` (no identity needed), write with `store_finding`, and revise with `update_finding`; `supersede` links require the AGE backend. Agent-facing discipline: [`../../skills/knowledge-graph/SKILL.md`](../../skills/knowledge-graph/SKILL.md).
- **Review (dialectic)** — a structured protocol (thesis → antithesis →
  synthesis) that an agent or operator requests with `request_review`, for
  disputed work or a disputed decision. A review completes when its session resolves.
- **Outcomes** — `record_result` stores test, build, exit-code or review
  results. Evidence from CI or an operator is stronger than an agent-authored
  outcome; if the agent controls both its confidence and every result record,
  it can forge a calibrated story.

## 1.5 The policy action

Each `sync_state` check-in returns an effective action and preserves the raw
recommendation separately. `action_summary.action` is `proceed` or `pause`,
qualified by `sub_action`:

| `action` / `sub_action` | Meaning | What the agent should do |
|---|---|---|
| `proceed` | No lifecycle hold is applied | Continue working normally; this is not a safety certification |
| `proceed` / `guide` | Continuation with guidance | Read the guidance text and adjust approach |
| observed `pause` recommendation (default) | The instrument crossed a policy edge, but `decision.action` remains `proceed` / `guide` and `enforcement.applied=false` | Inspect if useful; no permission or recovery step is required |
| applied `pause` (`UNITARES_POLICY_ACTUATION=enforce`) | Compatibility hard stop: check-ins and new shared-memory entries are refused, not queued | Stop and read the reason; `self_recovery(action="check")` reports self-recovery eligibility |
| `pause` / `reject` | Legacy or review label for significant concern, normalized to `pause` | Request a dialectic review (`request_review`) to resolve or contest, or bring in a human |

Only an **applied** pause holds governed writes until self-recovery, review, an
operator or expiry lifts it. Authentication, authorization, archive state,
existing durable pauses, and short loop cooldowns remain independent of this
posture. Verdict definitions live in
[`src/governance_glossary.py`](../../src/governance_glossary.py).

## 1.6 The check-in instrument: EISV

The policy recommendation is derived from EISV, a behavioral state estimate computed at
each check-in. EISV ships in the core path today and moves to an optional
module subscribed to check-ins under a
[staged plan](../proposals/active/eisv-core-boundary-v0.md). You do not need
its vocabulary to use the record; it matters when you want to act on *why* a
check-in returned the action it did.

Each check-in returns four indices built from runtime observations and
controller history:

| | Historical name | What the index contains |
|---|---|---|
| **E** | Energy | Prior decision scores, controller coherence level and complexity divergence, with available outcome, continuity and tool features |
| **I** | Integrity | Calibration error and controller coherence trend, with available outcome-score consistency, continuity and tool-variety features |
| **S** | Entropy | Drift, regime transitions and complexity divergence, with available continuity and tool features |
| **V** | Valence | A separately smoothed raw `E_obs − I_obs` gap |

The historical names carry intended interpretations; the implemented indices
are defined by their inputs and transformations. Check-ins 1–2 use a cold-start
prior, check-ins 3–24 use fixed behavioral thresholds, and from check-in 25 risk
scoring is also relative to the agent's own history. Prior decisions feed back
into E, so a policy response can change later readings without a change in task
inputs.

The policy action also carries a **margin** (`settling` / `comfortable` / `tight` / `warning` / `critical`): proximity to the nearest measurable decision threshold (risk, void or coherence). Read `margin_scope` and `unmeasurable_edges` with it.

Two related fields need care. **Coherence** is an overloaded compatibility
field: read `coherence_source` and `coherence_role` with it. The default
`legacy_tanh_v` value is directional ODE control feedback, while the dormant
manifold producer is a structural E/I/S measurement; neither is an outcome or
health score. **Ethical drift** is a four-signal vector (calibration deviation,
complexity divergence, change in the configured coherence signal, stability
deviation) that feeds S and inherits the coherence producer's semantics; it is
not independent outcome evidence.

[Chapter 5](05-reading-the-signals.md#55-the-eisv-instrument) and
[`../EISV_COMPUTATION.md`](../EISV_COMPUTATION.md) give the formulas,
provenance and a worked example.

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
