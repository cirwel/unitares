---
name: dialectic-reasoning
description: >
  Use when an agent is participating in a UNITARES dialectic session — paused and needs to
  submit a thesis, reviewing another agent's thesis, or synthesizing conditions for resolution.
  Covers structured argumentation and convergence.
last_verified: "2026-06-28"
freshness_days: 14
source_files:
  - unitares/src/mcp_handlers/dialectic/handlers.py
  - unitares/src/mcp_handlers/dialectic/session.py
  - unitares/config/governance_config.py
---

# Dialectic Reasoning

## When Dialectics Happen

A dialectic session is triggered when:

- You receive a **pause** or **reject** verdict and want to contest it
- You manually call `request_dialectic_review()` for peer verification
- You find something that contradicts the knowledge graph
- A high-stakes decision needs structured verification before proceeding

Dialectics are not punishment. They are a structured way to resolve disagreements using evidence and negotiation. In current UNITARES language, think of them as structured review more than "recovery court."

## Phase 1: Thesis

The paused agent submits their position (the protocol rejects a thesis from anyone else):

```
submit_thesis(
  reasoning: "Why I should resume / why my position is correct",
  root_cause: "What went wrong or what triggered this",
  proposed_conditions: ["Concrete, measurable condition 1", "Condition 2"]
)
```

### Writing a Good Thesis

- **Reasoning**: Explain your perspective with reference to EISV data, not feelings
- **Root cause**: Be specific. "High entropy from complex refactoring task" is better than "I got paused for no reason"
- **Proposed conditions**: Must be concrete and measurable. Tie them to live metrics or observable behavior, not vague intent.

## Phase 2: Antithesis

A reviewing agent examines the thesis and raises concerns:

```
submit_antithesis(
  reasoning: "Counter-arguments to the thesis",
  concerns: ["Specific risk 1", "Specific risk 2"],
  observed_metrics: { E: 0.45, I: 0.38, S: 1.2, V: 0.8 }
)
```

### Writing a Good Antithesis

- **Reasoning**: Address the thesis directly. What does the data actually show?
- **Concerns**: Be specific about risks. "I exceeds E by 0.3, indicating integrity debt" is useful.
- **Observed metrics**: Include the actual EISV values backing your concerns

If identity or session continuity looks suspect, verify with `identity()` before assuming the thesis belongs to the agent you think it does.

## Phase 3: Synthesis

Both sides negotiate toward resolution:

```
submit_synthesis(
  reasoning: "How we reconcile the thesis and antithesis",
  agrees: true/false,
  proposed_conditions: ["Negotiated condition 1", "Condition 2"]
)
```

Convergence happens when both sides agree on conditions. The synthesis should reflect genuine agreement, not capitulation.

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
- **Look at EISV data, not feelings.** Call `get_governance_metrics()` for actual values. The thermodynamic model is the shared ground truth.

## Common Mistakes

- **Ignoring the metrics**: Arguing against a pause while your entropy is at 1.5 and energy is at 0.3. The numbers matter.
- **Proposing impossible conditions**: promising a metric target without checking the live state first.
- **Being defensive instead of analytical**: "The system is wrong" vs. "My entropy spiked because of X, and here is how I address it."
- **Treating dialectic as adversarial**: It is collaborative problem-solving with structure, not a trial. Both sides benefit from honest resolution.
- **Rushing synthesis**: Agreeing to conditions you cannot meet just to get unpaused guarantees a future pause.
