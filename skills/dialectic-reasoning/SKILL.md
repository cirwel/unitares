---
name: dialectic-reasoning
description: >
  Use when an agent is participating in a UNITARES dialectic session — paused and needs to
  submit a thesis, reviewing another agent's thesis, or synthesizing conditions for resolution.
  Covers structured argumentation and convergence.
last_verified: "2026-09-23"
freshness_days: 28
source_files:
  - unitares/src/dialectic_protocol.py
  - unitares/src/mcp_handlers/dialectic/handlers.py
  # Added 2026-09-14: participant resolution and the same-bound-caller gate
  # for thesis, antithesis, and synthesis live here.
  - unitares/src/mcp_handlers/dialectic/auth.py
  - unitares/src/mcp_handlers/dialectic/session.py
  - unitares/src/mcp_handlers/dialectic/responses.py
  - unitares/src/mcp_handlers/dialectic/auto_resolve.py
  - unitares/src/mcp_handlers/dialectic/reviewer.py
  - unitares/src/mcp_handlers/dialectic/enforcement.py
  - unitares/src/mcp_handlers/schemas/dialectic.py
  - unitares/src/mcp_handlers/tool_stability.py
  - unitares/src/mcp_handlers/identity/operator.py
  - unitares/src/mcp_handlers/lifecycle/query.py
---

# Dialectic Reasoning

## When Dialectics Happen

A dialectic session is useful when:

- You receive a **pause** or **reject** verdict and want to contest it
- You call `request_review()` for peer or model-assisted verification
- You find something that contradicts the knowledge graph
- A high-stakes decision needs structured verification before proceeding

Dialectics are not punishment. They are a structured way to resolve disagreements using evidence and negotiation. In current UNITARES language, think of them as structured review more than "recovery court."

## Bind Before You Submit

`thesis`, `antithesis`, and `synthesis` are state-changing participant actions.
Each requires the identity resolver to have bound the caller, and the UUID being
submitted must equal that resolver-stamped caller. The normal flow is to bind
with `start_session` or `identity`, then omit `agent_id`; the transport carries
the bound UUID into the handler. Supplying an explicit `agent_id` is not
delegation and cannot authorize acting as another participant.

This is an equality gate, not an independent authentication protocol. Its
proof strength is exactly the strength of the upstream binding source and the
deployment's strict-identity policy. A server-inferred weak binding that the
policy accepts remains weak; the dialectic check must not be described as
proving strong identity or closing impersonation by itself. Inspect the
identity-assurance fields when that distinction matters.

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

### One-call review (the friendly default)

You do not have to learn the request→thesis protocol first. The friendly
`request_review` tool reuses `issue_description` as the thesis when no separate
thesis fields are supplied, so a complete review brief is actionable as-is:

```
request_review(
  issue_description: "What you want verified, your position, and the evidence"
)
```

Pass `reasoning` (and optionally `root_cause` / `proposed_conditions`) when the
position should be distinct from the subject. Pass `use_brief_as_thesis=false`
for a neutral request that deliberately waits for a later thesis. The raw
`dialectic(action="request")` path preserves that two-call default unless you
pass thesis fields or `use_brief_as_thesis=true`.

For a session already waiting in the thesis phase, reuse what the server saved
instead of copying it back:

```
dialectic(
  action: "thesis",
  session_id: "<session-id>",
  use_brief_as_thesis: true
)
```

`root_cause` is always required. Recovery sessions additionally require at
least one concrete `proposed_condition`; manual review sessions may let the
reviewer propose conditions if needed.

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

### Stamp where the verdict came from

If the review was produced **outside the server** — a Codex or other-model
consult, a subagent council, any reviewer that is not this MCP — file it with
`reviewer_provenance` so it becomes a governed record rather than an untracked
opinion:

```
dialectic(
  action: "antithesis",
  session_id: "<session-id>",
  reasoning: "<the counter-perspective or observations; required>",
  concerns: ["<one per finding>"],
  observed_metrics: {},
  reviewer_provenance: {
    reviewer_kind: "external_consult",   # see the caution below
    backend: "codex-cli",
    model_used: "<model>",
    consult_source: "<what invoked it>"
  }
)
```

⛔**A misspelled `reviewer_kind` silently becomes `agent_submitted`** — the
server falls back rather than erroring (`handlers.py`,
`kind if kind in _REVIEWER_KINDS else "agent_submitted"`). So a typo does not
fail loudly; it files your outside consult as if you had reviewed it yourself,
which is the one misattribution this field exists to prevent. The agent-suppliable
kinds are exactly `agent_submitted`, `external_consult`, and `orchestrated`
(`in_process_synthetic` is the server's own stamp — do not send it).

Unrecognised keys are dropped (the response lists them), and string values are
truncated at 200 characters. Useful ones beyond the example: `model_requested`,
`models_used`, `tokens_used`, `cost_usd`, `latency_ms`, `finish_reason`,
`fallback_from`, `warnings`, `consulted_at`.

**Why this matters, measured 2026-08-28.** The unitares README records "Benefit
from review and coordination — **Untested**", and names `reviewer_provenance` as
how outside review becomes countable. Across all 669 recorded dialectic
messages, 3 carry `agent_submitted` and **0 carry `external_consult`** — the
path has never been used. Review sessions themselves are routine (136), so this
is not a missing mechanism: it is an optional field nobody fills in. Every
unfiled outside review leaves the record unable to move, however much review
actually happens.

- **Stamp it, don't infer it.** The field is descriptive, not identity proof.
  Record the backend and model that actually ran.
- **Mark failures as failures.** Set `degraded: true` when the consult errored
  or timed out but still reached a judgment you are filing. A failed pass filed
  as a clean one is worse than no row at all.
- **If no judgment was reached, do not file one.** When the consult returned
  nothing you could read as a verdict, pass `judgment_formed: false` instead.
  The server records an abstention without claiming or changing reviewer-slot
  ownership; the slot is OPEN only when no reviewer was already assigned. It does
  not file a rejection. `degraded` describes the BACKEND, `judgment_formed`
  says whether a verdict exists at all, and the two are not interchangeable —
  a model that judged and then failed its output format is degraded but HAS
  judged, and that objection is real and must still be filed. Filing a
  non-verdict claims the one reviewer slot and blocks the paused agent behind
  reasoning nobody can act on (live: session `99ff6f25a310d23e`, 2026-09-19).
- **Never backfill.** File only a review this session actually performed;
  filing past passes after the fact fabricates governed history.

If identity or session continuity looks suspect, verify with `identity(client_session_id=...)` before
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

- **`whose_move`** — plain language, for example `"YOURS — your thesis is
  owed; the saved brief can be reused"`, `"the reviewer's — their antithesis is
  owed"`, `"a reviewer's — the slot is OPEN, you may claim it"`, `"YOURS —
  respond once to the reviewer's standing rejection"`, or `"nobody — session is
  terminal"`.
- **`next_call`** — a ready-to-use call template, present only when the move is
  actually yours. If `next_call` is null, you are waiting on someone else.
- **`wait_assessment`** — `{elapsed_s, expected_by_s, assessment, poll_after_s, note}`. The
  same misreading recurs on the time axis: a session awaiting an orchestrated
  reviewer looked identical at 72 seconds and at 72 minutes until this field
  existed, and on 2026-09-13 that produced two wrong "the reviewer is absent"
  calls inside one session, against a reviewer that was mid-model-call and
  arrived at ~2m20s. `expected_by_s` is derived from the reviewer's own
  configured ceiling (`UNITARES_DIALECTIC_CODEX_TIMEOUT_S`, plus spawn or
  reconsider overhead).

  `too_early` means the wait is unremarkable — **not** that the reviewer is
  alive. A reviewer inside its budget may already be gone and merely not yet
  late, so an absence read here is not evidence. Only `overdue` is evidence, and
  it is evidence to look rather than to conclude. `poll_after_s` is the
  remaining budget as a number to wait on, set only while `too_early`; it is
  null when overdue (look, don't poll again) and whenever no budget exists.

  A deadline is asserted only when somebody actually owes the move and the
  server can say who and since when: an identified orchestrated reviewer holds
  an outstanding obligation, and the transcript carries a usable clock.
  Otherwise `assessment` is `null` and `note` says which condition was missing.
  `null` therefore covers four different situations — nobody owes a reviewer
  turn (terminal session, your own turn, or an open unclaimed slot), the
  reviewer is a human or otherwise unmanaged and has no declared budget to
  exceed, the transcript clock is missing or unreadable, or its entries are not
  in causal order. None of them is a sign of health, and none of them is a sign
  of trouble either. Read `note` rather than inferring from the `null`.

  Do not read an OPEN slot as a late reviewer. No agent has taken the
  obligation, so there is no budget to exceed and the field declines to invent
  one; an unclaimed slot at 72 minutes is an unanswered invitation, not a
  missing reviewer.

  Do not reassign or ask for facilitation from a `too_early` or `null`
  assessment. Wait and read `note`; only `overdue` supports investigating or
  offering the operator a facilitation path, and even then it does not prove
  that the reviewer is gone.

Read `whose_move` before concluding a session is hung, and `wait_assessment`
before concluding it is late. Use
`dialectic(action="get", session_id="...", check_timeout=true)` when you need the
latest timeout/facilitation state; note that `check_timeout` is a write (it can
auto-reassign or flip the phase) and is silently ignored for an unbound caller. An open reviewer slot in the antithesis phase
is an invitation, not a stall — an eligible agent other than the paused agent
may claim it.

## Facilitation and Reviewer Recovery

If no eligible reviewer remains, the session may report
`awaiting_facilitation`. This is a paused request for human help, not a reviewer
verdict. A timeout sweep can eventually mark it failed, but that sweep outcome
does not mean either side won.

A session that stalls in SYNTHESIS while the move is its reviewer's (the first
synthesis verdict is still owed, or the paused agent has
answered its standing objection) raises the
same request; the sweep does not reassign at that phase, because the protocol
requires the same reviewer to revise its own verdict. Once raised, the request
lets `dialectic(action="get", check_timeout=true)` replace a reviewer that is
paused or missing, as it already can after a standing objection; a reviewer
whose status still reads active waits for an operator `reassign`. A SYNTHESIS
stall where the paused agent owes the next move when the sweep finds it raises
no request and is failed at the stuck threshold. A request is not withdrawn
when the reviewer later answers, so a session flagged while the reviewer owed
the move stays on the 4h hold after the move passes back.

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
| **resume** | Converged; the agent continues with the agreed conditions |
| **block** | The agreed resolution violated a hard safety limit; the session is marked `failed` |
| **facilitation_required** / `awaiting_facilitation` | Standing reviewer rejection, self-review, or no eligible reviewer; an operator or replacement reviewer must act |

`escalate` and `cooldown` exist in the `ResolutionAction` enum but no live path
produces them; they survive only as recommendation labels in the model-assisted
tool.

A resolved session read also carries an `attestation` block saying what party
signatures the record actually holds: `bilateral`, `single_signer`, `unsigned`,
or `legacy_v1`. Read it rather than inferring from the signature fields, and do
not read `signature_version` as a claim that anyone signed — it names the
scheme, not the act. As of 2026-09-08 essentially every live resolution is
`unsigned`, because agents are no longer minted with the api_key the party HMAC
needs. That is a known gap under decision, not a fault in your session, and it
does not weaken the conditions you agreed to: the resolution record is governed
by the transcript either way.

## How to Participate Well

- **Be honest about metrics.** The EISV data is right there. Denying it undermines your credibility.
- **Propose measurable conditions.** "Entropy below X within Y check-ins" is verifiable. "I will be more careful" is not.
- **Do not be defensive.** A pause verdict is data, not judgment. Analyze why it happened.
- **Acknowledge valid concerns.** If the antithesis raises a real issue, say so. Partial agreement strengthens your position.
- **Look at attributed evidence, not feelings.** Call `check_working_state()`
  (`get_governance_metrics()` canonically) for the current values and inspect
  `risk_score_source` and `verdict_source` / `verdict_resolution_source`; the
  `policy_evaluation` and `enforcement` blocks come back on your last
  `sync_state()` check-in, not on the metrics call. EISV is proprioceptive
  state estimation, not outcome truth; the ODE lens and legacy `C(V)` do not
  independently validate a claim.

## Common Mistakes

- **Ignoring the metrics**: Arguing against a pause while your entropy is near 1.0 and energy is at 0.3. The numbers matter.
- **Proposing impossible conditions**: promising a metric target without checking the live state first.
- **Being defensive instead of analytical**: "The system is wrong" vs. "My entropy spiked because of X, and here is how I address it."
- **Treating dialectic as adversarial**: It is collaborative problem-solving with structure, not a trial. Both sides benefit from honest resolution.
- **Rushing synthesis**: Agreeing to conditions you cannot meet just to get unpaused leaves the reading that paused you in place, so the next check-in can pause again. An unmet condition itself only guides.
