---
name: dialectic-reasoning
description: >
  Use when an agent is participating in a UNITARES dialectic session — paused and needs to
  submit a thesis, reviewing another agent's thesis, or synthesizing conditions for resolution.
  Covers structured argumentation and convergence.
last_verified: "2026-08-16"
freshness_days: 28
source_files:
  - unitares/src/mcp_handlers/dialectic/handlers.py
  - unitares/src/mcp_handlers/dialectic/session.py
  - unitares/src/mcp_handlers/dialectic/responses.py
  - unitares/src/mcp_handlers/dialectic/auto_resolve.py
  - unitares/src/mcp_handlers/schemas/dialectic.py
  - unitares/config/governance_config.py
---

# Dialectic Reasoning

## When Dialectics Happen

A dialectic session is useful when:

- You receive a **pause** or **reject** verdict and want to contest it
- You call `request_review()` for peer or model-assisted verification
- You find something that contradicts the knowledge graph
- A high-stakes decision needs structured verification before proceeding

Dialectics are not punishment. They are a structured way to resolve disagreements using evidence and negotiation. In current UNITARES language, think of them as structured review more than "recovery court."

## Phase 1: Thesis

The paused agent submits their position (the protocol rejects a thesis from
anyone else):

```
dialectic(
  action: "thesis",
  session_id: "<session-id>",
  reasoning: "Why I should resume / why my position is correct",
  root_cause: "What went wrong or what triggered this",
  proposed_conditions: ["Concrete, measurable condition 1", "Condition 2"]
)
```

### One-call review (usually what you want)

You do not have to learn the request→thesis protocol first. If you pass the
thesis fields to the request itself, the thesis is submitted in the same call
and you get back a review verdict (or a dispatched reviewer) directly:

```
request_review(
  issue_description: "What you want verified",
  root_cause: "What went wrong or what triggered this",
  reasoning: "Why your position is correct",
  proposed_conditions: ["Concrete condition 1", "Condition 2"]
)
```

Passing either `reasoning` or `root_cause` triggers this. Omit both and you get
the unchanged two-call flow (request, then `thesis`), which is still valid.

### Writing a Good Thesis

- **Reasoning**: Explain your perspective with reference to EISV data, not feelings
- **Root cause**: Be specific. "High entropy from complex refactoring task" is better than "I got paused for no reason"
- **Proposed conditions**: Must be concrete and measurable. Tie them to live metrics or observable behavior, not vague intent.

## Phase 2: Antithesis

A reviewing agent examines the thesis and raises concerns:

```
dialectic(
  action: "antithesis",
  session_id: "<session-id>",
  reasoning: "Counter-arguments to the thesis",
  concerns: ["Specific risk 1", "Specific risk 2"],
  observed_metrics: { E: 0.45, I: 0.38, S: 0.62, V: 0.18 }
)
```

### Writing a Good Antithesis

- **Reasoning**: Address the thesis directly. What does the data actually show?
- **Concerns**: Be specific about risks. "I exceeds E by 0.3, indicating integrity debt" is useful.
- **Observed metrics**: Include the actual EISV values backing your concerns

If identity or session continuity looks suspect, verify with `identity()` before
assuming the thesis belongs to the agent you think it does. An independent
reviewer also receives server-captured pause evidence separately from the paused
agent's narrative. Treat its measurement, policy, and enforcement provenance as
context; legacy `C(V)` is diagnostic controller feedback, not independent
evidence that the thesis is right or wrong.

## Phase 3: Synthesis

Both sides negotiate toward resolution:

```
dialectic(
  action: "synthesis",
  session_id: "<session-id>",
  reasoning: "How we reconcile the thesis and antithesis",
  agrees: true/false,
  proposed_conditions: ["Negotiated condition 1", "Condition 2"]
)
```

For an independent review, the reviewer owes the first synthesis verdict before
the paused agent can answer it. Convergence happens only when the required sides
agree on conditions. A reviewer rejection remains in force: the paused agent
cannot clear it by repeatedly submitting `agrees=true`. The reviewer must revise
after new evidence, or an authorized facilitator must reassign the reviewer.

## Whose Move Is It?

A session waiting on *you* looks identical to a stalled session if you only read
the phase field — on 2026-07-28 a live session awaiting the caller's own
synthesis was read as "stalled" by two experienced operators. So status
responses answer it directly from your seat:

- **`whose_move`** — plain language: `"YOURS — your thesis is owed"`,
  `"the reviewer's — their antithesis is owed"`, `"a reviewer's — the slot is
  OPEN, you may claim it"`, or `"nobody — session is terminal"`.
- **`next_call`** — a ready-to-use call template, present only when the move is
  actually yours. If `next_call` is null, you are waiting on someone else.

Read `whose_move` before concluding a session is hung. Use
`dialectic(action="get", session_id="...", check_timeout=true)` when you need the
latest timeout/facilitation state. An open reviewer slot in the antithesis phase
is an invitation, not a stall — an eligible agent other than the paused agent
may claim it.

## Facilitation and Reviewer Recovery

If no eligible reviewer remains, the session may report
`awaiting_facilitation`. This is a paused request for human help, not a reviewer
verdict. A timeout sweep can eventually mark it failed, but that sweep outcome
does not mean either side won.

Reviewer reassignment is privileged:

```
dialectic(
  action: "reassign",
  session_id: "<session-id>",
  new_reviewer_id: "<agent-id>",
  reason: "Reviewer unavailable; continue the review"
)
```

Only an authenticated operator or the currently assigned independent reviewer
handing off their own slot may do this. A merely bound agent identity is not
operator authority. Reassignment can revive an `awaiting_facilitation` session,
including one a timeout sweep already marked failed for lack of facilitation.

## Resolution Outcomes

| Outcome | Meaning |
|---------|---------|
| **resume** | Agent continues with agreed conditions |
| **block** | Agent stays paused — conditions not met or agreement not reached |
| **escalate** | Needs human/operator intervention |
| **cooldown** | Temporary pause — retry after a delay |

## How to Participate Well

- **Be honest about metrics.** The EISV data is right there. Denying it undermines your credibility.
- **Propose measurable conditions.** "Entropy below X within Y check-ins" is verifiable. "I will be more careful" is not.
- **Do not be defensive.** A pause verdict is data, not judgment. Analyze why it happened.
- **Acknowledge valid concerns.** If the antithesis raises a real issue, say so. Partial agreement strengthens your position.
- **Look at attributed evidence, not feelings.** Call `check_working_state()` for
  the current values and inspect `risk_score_source`, policy provenance, and
  enforcement evidence. EISV is proprioceptive state estimation, not outcome
  truth; the ODE lens and legacy `C(V)` do not independently validate a claim.

## Common Mistakes

- **Ignoring the metrics**: Arguing against a pause while your entropy is near 1.0 and energy is at 0.3. The numbers matter.
- **Proposing impossible conditions**: promising a metric target without checking the live state first.
- **Being defensive instead of analytical**: "The system is wrong" vs. "My entropy spiked because of X, and here is how I address it."
- **Treating dialectic as adversarial**: It is collaborative problem-solving with structure, not a trial. Both sides benefit from honest resolution.
- **Rushing synthesis**: Agreeing to conditions you cannot meet just to get unpaused guarantees a future pause.
