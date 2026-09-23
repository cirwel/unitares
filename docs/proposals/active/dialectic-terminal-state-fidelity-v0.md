# Dialectic terminal-state fidelity — what `failed` does not distinguish

**Status:** decision packet, raised 2026-09-13. **Nothing here is implemented.**
No enum, migration, threshold or production gate changes. The *Default if
silent* clause describes the current unimplemented state, which is a working
system — this is a fidelity question, not an outage.
**Shape:** follows `docs/proposals/archive/operator-decision-packet-v0.md` — options,
recommendation, reversibility and blast radius, default-if-silent.
**Raised by:** a Claude Code session, branch
`claude/pruning-consolidation-refactor-5ta5ou`, from dogfooding the readiness
gate on PR #2191.
**Evidence provenance:** agent-authored analysis of the cited repository sources
and one live dialectic session. The recommendation is this session's lean, not a
council finding and not a dialectic resolution.
**Related:** issue #2202 (the reviewer does not return after disagreement).
That issue asks *how a stalled review should be rescued*; this packet asks *what
the record should say when it is not*. They are separable and this one is
cheaper.

---

## The question

`DialecticPhase.FAILED` is the terminal state for at least four distinct
outcomes. Its own docstring says so: *"FAILED: Session failed (timeout, max
rounds exceeded, error, etc.)"*.

Those outcomes are not equivalent to a reader:

| What happened | What the record says |
|---|---|
| The reviewer considered the response and the objection stood | `failed` |
| Synthesis rounds were exhausted without convergence | `failed` |
| The reviewer stopped responding and never saw the response | `failed` |
| A write or protocol error killed the session | `failed` |

Only the first two are *adjudications*. The third is an **availability event**
and the fourth is a **defect**, and neither says anything about the claim under
review.

**Why this is a federation question rather than a tidiness one.**
`docs/PRODUCT_DEFINITION.md` frames the kernel as five questions. Four of them —
who said it, what supports it, what happened, what a successor can recover — are
records: one runtime writes, others read. **Question 3, "who challenged it?", is
the only one where two independent runtimes must interact and reach a state
neither controls alone.** That is the federation primitive; the rest is
accountability storage. Its stated boundary is *"A reviewer must participate;
requesting review is not completing review."*

So when Q3 terminates, the terminal label is the durable answer to the kernel's
one distinctively federated question. A successor performing Q5 reconstruction
reads `failed` and cannot tell whether a peer rejected the claim or a process
died. That is not a missing nicety — it is the shared accountability record
failing to record the thing it exists to record.

## What is and is not broken

Established by reading the code, not inferred:

- A reviewer that disagrees and then stops responding leaves the session in
  SYNTHESIS with `awaiting_facilitation=true`
  (`src/mcp_handlers/dialectic/handlers.py`, `SELF_CLEAR_REFUSED`).
- The sweep holds such a session on the operator's clock until
  `FACILITATION_TIMEOUT` (4h), deliberately — `auto_resolve.py:564-577` explains
  that an operator "may simply be asleep."
- After that it falls through to `update_session_status_async(session_id,
  "failed")` (`auto_resolve.py:586-588`).
- **It does not become unfacilitatable.** `handlers.py:3598` permits `reassign`
  outside THESIS/ANTITHESIS when `awaiting_facilitation` is set, and the dormant
  registry records that reassignment can revive a session a timeout sweep
  already marked failed. An earlier dead-end at this seam (38 sessions, 6 real
  requests) was fixed; that comment is history, not a live bug.

So the loop is not stuck-forever and the operator escape works. **What is wrong
is only the label** — which is exactly why this is cheap.

Live instance: dialectic session `490c7cf515b89a6e`, 2026-09-13. Reviewer
`DialecticReviewer_66616d1b` (codex backend, not degraded) returned
`agrees=false` with three substantive conditions at 18:46:10Z. The paused agent
answered at 18:51:57Z with all three satisfied. The reviewer never returned. On
the present path that session terminates as `failed`, and the record will not
distinguish it from a review that considered the answer and rejected it.

## Options

**A. Add a distinct terminal state for reviewer non-completion** *(recommended)*

Introduce one new terminal phase — e.g. `ABANDONED` — written where the sweep
currently writes `failed` for a session whose reviewer stopped responding after
submitting a verdict. `failed` keeps its meaning for exhausted rounds and
errors.

- *Consequence:* the record distinguishes adjudication from availability. Q5
  reconstruction can say "no peer completed this challenge" rather than implying
  a verdict that was never reached. Operator triage separates "a disagreement I
  should look at" from "a reviewer that died."
- *Tradeoff:* a new enum value is a wire-visible change. Every consumer that
  branches on phase, and `_TERMINAL_PHASES` in
  `agents/dialectic_reviewer/reviewer.py`, must learn it; an older client
  reading an unknown phase must degrade sanely. Needs a migration decision for
  existing `failed` rows (recommendation: **do not backfill** — the distinction
  was not recorded, and inventing it retroactively is exactly the
  evidence-fabrication this repo's registry exists to prevent).

**B. Keep one terminal state, add a structured reason field**

Leave `failed` as the phase; record `terminal_reason` beside it
(`objection_stood` / `rounds_exhausted` / `reviewer_abandoned` / `error`).

- *Consequence:* same information, no wire-visible enum change, no client
  breakage. Cheaper and fully reversible.
- *Tradeoff:* the distinction lives in a field that consumers must opt into
  reading, so every existing `phase == failed` branch stays as wrong as it is
  today until each is updated. Fidelity is available rather than enforced — the
  failure mode this repo names repeatedly, where the evidence exists but not at
  the level anyone reads.

**C. Do nothing; treat the label as adequate**

- *Consequence:* no work, no risk. The operator continues to reconstruct intent
  from the transcript, which does carry the full story.
- *Tradeoff:* the transcript is per-session and human-read; the phase is what
  aggregates, what sweeps branch on, and what a successor's automated
  reconstruction sees. Accepting C is accepting that the kernel's answer to its
  own Question 3 is "something ended it."

## Recommendation

**A**, with **B as the fallback if wire compatibility is judged too expensive.**

The reasoning is not that A is tidier. It is that this repo's recurring defect —
documented in `docs/operations/dormant-capability-registry.md` and hit four
separate times in the session that raised this packet — is *two states collapsed
into one signal, resolved toward the wrong reading*. Every other instance of it
is recoverable by re-deriving the evidence. **This one is not: the terminal
write is the permanent record, and the distinction is unrecoverable after the
fact.** That asymmetry is what argues for the enforced form over the opt-in one.

Explicitly **not** recommended, and argued against: auto-spawning a replacement
reviewer to close the session. A replacement carries a new identity that never
formed the original objection, so letting it revise the verdict records a
completed review that no reviewer completed. For an accountability kernel a
dishonestly-complete record is worse than an honestly-incomplete one. Stranding
is at least true. (This revises the framing in #2202, which presented that
option neutrally.)

## Reversibility and blast radius

| Field | Value |
|---|---|
| **class** | `authority` — it changes what the shared record asserts, not merely how it is displayed |
| **reversibility** | A: `costly` (a shipped enum value is hard to withdraw once clients read it). B: `reversible` |
| **blast_radius** | `surface` — the dialectic terminal-write path and every phase consumer. Not `fleet`: no resident behavior, no scoring, no policy gate reads terminal phase |

## Default if silent

Nothing changes. Sessions whose reviewer abandons them continue to terminate as
`failed`, indistinguishable from a rejection on the merits. The operator escape
(`dialectic(action='reassign', ...)`) keeps working, so no session is lost — the
record simply keeps under-reporting what happened, and each such row is
unrecoverable once written.

## Decision points, if A is chosen

1. **The name.** `ABANDONED` is descriptive of the reviewer, not the claim, which
   is the intent. Alternatives worth rejecting deliberately: `INCOMPLETE` (true
   but says nothing about who), `TIMEOUT` (an implementation cause, not an
   outcome).
2. **Backfill: no.** Recommended above; worth an explicit call rather than a
   silent choice.
3. **Unknown-phase handling in clients**, including `_TERMINAL_PHASES` in the
   reviewer runner, which must treat the new state as terminal or the runner will
   poll a dead session for its full continuation window.
4. **Whether `reassign` may revive it.** It may revive `failed` today. Keeping
   that for the new state preserves the operator escape; forbidding it would make
   the state more final than the one it replaces, which is not the intent.
