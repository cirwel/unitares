---
name: dialectic-reasoning
description: >
  Use when an agent chooses to participate in a UNITARES dialectic session —
  requesting structured review, submitting a thesis, reviewing another agent's
  thesis, or synthesizing conditions for resolution. Covers structured
  argumentation and convergence.
last_verified: "2026-11-05"
freshness_days: 28
source_files:
  - unitares/docs/EISV_COMPUTATION.md
  - unitares/src/agent_loop_detection.py
  - unitares/src/mcp_handlers/middleware/envelope_step.py
  - unitares/src/dialectic_protocol.py
  - unitares/src/dialectic_db.py
  - unitares/src/dialectic_reviewed_work.py
  - unitares/db/postgres/migrations/075_dialectic_resolution_effects.sql
  - unitares/db/postgres/migrations/077_dialectic_transcript_revision.sql
  - unitares/db/postgres/migrations/078_dialectic_resolution_saga_revision.sql
  - unitares/src/mcp_handlers/dialectic/handlers.py
  # Added 2026-09-14: participant resolution and the same-bound-caller gate
  # for thesis, antithesis, and synthesis live here.
  - unitares/src/mcp_handlers/dialectic/auth.py
  - unitares/src/mcp_handlers/dialectic/session.py
  - unitares/src/mcp_handlers/dialectic/responses.py
  - unitares/src/mcp_handlers/dialectic/read_projection.py
  - unitares/src/mcp_handlers/dialectic/auto_resolve.py
  - unitares/src/mcp_handlers/dialectic/reviewer.py
  - unitares/src/mcp_handlers/dialectic/resolution.py
  - unitares/src/mcp_handlers/dialectic/enforcement.py
  - unitares/src/mcp_handlers/schemas/dialectic.py
  - unitares/src/mcp_handlers/tool_stability.py
  - unitares/src/mcp_handlers/identity/operator.py
  - unitares/src/mcp_handlers/lifecycle/query.py
  - unitares/skills/dialectic-reasoning/references/facilitation-and-effects.md
---

# Dialectic Reasoning

## When Dialectics Happen

Use a dialectic session for structured, evidence-backed review when:

- You have an applied **pause** and choose structured review for recovery
- You receive an observational pause recommendation and want a second perspective
- You call `request_review()` for peer or model-assisted verification
- You find something contradicting the knowledge graph or need verification on a high-stakes decision

Dialectics are structured review, not punishment.

## Bind Before You Submit & Read Compact Progress

- `thesis`, `antithesis`, and `synthesis` require a bound caller (`start_session`
  or `identity`); omit `agent_id` so transport carries your bound UUID (an
  explicit `agent_id` cannot act for another participant). Check
  `identity_assurance` when binding strength matters.
- Check session progress with `dialectic(action="get", session_id="<id>", fields="compact")`
  (contract 2.3.0+): keeps turn-taking guidance, resolution, authorship,
  attestation, and effect status while omitting the full transcript (read the
  full transcript before forming a judgment or replying). `action="list"` with
  `fields="compact"` returns the tally view. One active session per agent
  applies (`SESSION_EXISTS` gives a read call, not permission for a second).

## Phase 1: Thesis

`request_review` is the friendly one-call default (reuses `issue_description` as
the thesis when no separate thesis fields are sent; pass
`use_brief_as_thesis=false` for a neutral two-call request, which raw
`dialectic(action="request")` uses by default):

```
request_review(issue_description="What you want verified, your position, and the evidence")
```

Or submit an explicit thesis (reusing the saved brief via `use_brief_as_thesis: true`
when already in the thesis phase; for an applied pause only the paused agent may
submit):

```
dialectic(
  action: "thesis",
  session_id: "<session-id>",
  reasoning: "Why I should resume / why my position is correct",
  root_cause: "Observed facts vs. hypothesis (always required)",
  proposed_conditions: ["Concrete, checkable condition 1"]  # required for recovery sessions
)
```

- **Proxy conflict recusal**: when filing on behalf of another UNITARES agent,
  pass every known author UUID in `reviewed_work_author_agent_ids=["<uuid>"]`
  (omit when unknown; empty list is refused). Declared authors are recused from
  independent review; malformed markers fail closed. Explicit self-review never
  counts as an independent seat and cannot close a pause-bound session.

## Phase 2: Antithesis & Outside Consults

An assigned reviewer submits an antithesis addressing the thesis directly with
specific concerns and observed EISV metrics:

```
dialectic(
  action: "antithesis",
  session_id: "<session-id>",
  reasoning: "Counter-arguments to the thesis",
  concerns: ["Specific risk 1", "Specific risk 2"],
  observed_metrics: { E: 0.45, I: 0.38, S: 0.62, V: 0.18 }
)
```

If a review came from **outside this server** (Codex, another model, a subagent
council), record it with `dialectic(action="consult")` (no reviewer slot
required; carries **no authority**, never advances phase or refreshes liveness):

```
dialectic(
  action: "consult",
  session_id: "<session-id>",
  reasoning: "<outside reviewer's argument; required>",
  agrees: false,
  proposed_conditions: ["<condition>"],
  concerns: ["<finding>"],
  reviewer_provenance: { backend: "codex-cli", model_used: "<model>", consult_source: "<invoker>" }
)
```

- ⛔ **Exact `reviewer_kind` spelling**: valid agent-suppliable kinds are
  `agent_submitted`, `external_consult`, and `orchestrated` (never
  `in_process_synthetic`). A typo silently falls back to `agent_submitted`
  without erroring!
- **`degraded` vs. `judgment_formed: false`**: mark backend errors/timeouts that
  still produced a verdict as `degraded: true`. If **no judgment** was formed,
  do not file a `consult` (`NO_JUDGMENT`); an assigned reviewer files
  `judgment_formed: false` on `antithesis` (records abstention without claiming
  the slot or filing a rejection). Never backfill past runs.

When filing `reviewer_provenance` extras, hitting the 8-consult limit, or
inspecting server-captured `paused_agent_state`, read
`references/facilitation-and-effects.md`
(remote MCP: `skills(name="dialectic-reasoning", reference="facilitation-and-effects.md")`).

## Phase 3: Synthesis

```
dialectic(
  action: "synthesis",
  session_id: "<session-id>",
  reasoning: "How we reconcile the thesis and antithesis",
  agrees: true/false,
  proposed_conditions: ["Negotiated condition 1"]
)
```

For an independent review, the reviewer owes the first synthesis verdict before
the paused agent can reply. A standing reviewer rejection cannot be cleared by
repeatedly submitting `agrees=true`: the reviewer must revise after new
evidence, an authorized facilitator must reassign, or the session closes as
`rejection_stands`.

## Whose Move Is It?

Read these fields before assuming a session is hung or late:

- **`whose_move`**: plain-language owner of the next turn (`"YOURS — ..."`,
  `"the reviewer's — ..."`, `"a reviewer's — the slot is OPEN, ..."`, or
  `"nobody — session is terminal"`).
- **`next_call`**: ready-to-use call template, present only when the move is
  yours (`null` when waiting on someone else).
- **`wait_assessment`**: `{elapsed_s, expected_by_s, assessment, poll_after_s, note}`:
  `too_early` is **not** proof the reviewer is alive; `null` is **not** approval
  or health (read `note`); an OPEN slot is an unanswered invitation, not a late
  reviewer. Only `overdue` supports investigating or requesting facilitation —
  do not reassign on `too_early` or `null`.

## Resolution Outcomes & Concurrency

| Outcome | Meaning |
|---------|---------|
| **resume** | Converged; the agent continues with the agreed conditions |
| **block** | Agreed resolution violated a hard safety limit; session marked `failed` |
| **facilitation_required** / `awaiting_facilitation` | Standing rejection, self-review, or no eligible reviewer; needs operator or replacement reviewer |
| **rejection_stands** | Reviewer's rejection stands after its last word; sweep closes `failed` (not an approval) |

- If a write returns `DIALECTIC_TRANSCRIPT_CHANGED` or
  `SESSION_RESOLUTION_IN_PROGRESS`, reload the session before acting.
- If a response reports an interrupted resolution effect (`pending`, `applying`,
  `unknown_partial`), do **not** repeat participant moves or assume the pause
  lifted — hand off to an operator (`dialectic(action="reconcile_effect")`).

When `wait_assessment` is `overdue` or `null`, `check_timeout=true`,
`awaiting_facilitation`, `reassign`, `reconcile_effect`, `legacy_unbound_session`,
or `attestation` (`unsigned_by_design`) appears, read
`references/facilitation-and-effects.md`
(remote MCP: `skills(name="dialectic-reasoning", reference="facilitation-and-effects.md")`).

## How to Participate Well

- **Report readings faithfully.** Separate observed values from hypotheses and
  identify what you dispute (input, provenance, interpretation, or policy).
- **Propose checkable conditions** tied to the task or evidence (a favorable
  metric number alone does not establish recovery).
- **Do not treat the instrument as a judge.** A pause recommendation is data,
  not permission or a moral verdict; acknowledge valid antithesis concerns.
- **Inspect attributed evidence**: use `check_working_state(client_session_id=...)`
  (`get_governance_metrics()` canonically) for current values and inspect
  `risk_score_source` / `verdict_source`; `policy_evaluation` and `enforcement`
  come from assessment-bearing `sync_state()` check-ins. EISV is proprioceptive
  state estimation (legacy `C(V)` does not validate outcome claims); see the
  [computation contract](https://github.com/cirwel/unitares/blob/master/docs/EISV_COMPUTATION.md#role-runtime-and-evidence).

## Common Mistakes

- **Treating a reading as conclusive** or assuming causality without checking inputs.
- **Proposing unsupported conditions** or rushing synthesis to agree to
  conditions you cannot meet (an unmet condition only guides, and the underlying
  state can pause you again).
- **Treating dialectic as adversarial**: it is structured collaborative problem-solving.
