# Operator Decision Packet — making load-bearing calls cheap to answer

- **Status:** Proposal (design-first; v0). No runtime code proposed in this doc.
- **Target:** UNITARES governance system (`CIRWEL/unitares`)
- **Problem:** Load-bearing decisions that *genuinely* require the operator
  (taste, authority transitions, irreversible risk) are a throughput bottleneck —
  not because the operator is in the loop, but because each one arrives as an
  undifferentiated "go read everything and decide" ask instead of a "pick one."
  The operator pays the archaeology cost on every call.

## What this is NOT

This is not a proposal to remove the operator from load-bearing decisions, and it
is not a council/dialectic that *auto-resolves* them. Those mechanisms can only
legitimately converge on decisions that *have* a ground truth — factual disputes,
coherence checks, "does the diff match the spec," reversible bets. A genuine
taste/authority/irreversible call has no ground truth for agents to converge on;
forcing a council to "decide" it just launders a guess as consensus.

The bottleneck for *this* class is packaging, not authority. The fix is to make
the operator's call a 10-second pick rather than a 30-minute dig.

## The class this addresses

From the escalation record, the operator-essential decisions are a recognizable
type:

- **Taste-level** — e.g. the three taste-level questions on S19 attestation
  (`plan.md`, 2026-05-03). No correct answer; only operator preference.
- **Authority transition** — e.g. `seeded → earned` lineage is "a single explicit
  operator action" (`r1-verify-lineage-claim.md`). The operator *is* the trust
  root; no delegate can stand in.
- **Irreversible / high-blast-radius** — fleet-bricking territory (the S21 session
  resolution incident), destination commitments (the BEAM `A′` decision,
  `beam-footprint-roadmap-v0.md`).

These *should* bottleneck on the operator. The cost to attack is the per-decision
overhead, not the decision count.

## Where this plugs in (the seam already exists, twice)

1. **The manual council pass** already does most of the upstream work — three
   subagents (`dialectic-knowledge-architect` + `code-reviewer` + `live-verifier`)
   converge on findings before a load-bearing change lands. Today the council
   terminates in *findings*; the operator still reads them and synthesizes the
   call. The packet is the council's output discipline: findings → a structured
   decision the operator picks from.

2. **The dialectic's `ESCALATE` action** (`src/dialectic_protocol.py:195`,
   "escalate to quorum") exists in the `ResolutionAction` enum but routes to
   `awaiting_facilitation` (i.e. the operator) today. That is exactly the seam
   where a typed, packaged escalation should be emitted instead of a bare
   "needs a human."

The packet is the common output contract for both paths.

## The Decision Packet contract

A load-bearing decision arrives at the operator as a structured object, never as
free-form context. Proposed shape:

```
decision_packet {
  decision_id:    uuid,
  title:          str,             # one line, the question in plain terms
  class:          enum(taste | authority | irreversible),
  reversibility:  enum(reversible | costly | one_way_door),
  blast_radius:   enum(local | surface | fleet),
  question:       str,             # the actual fork, stated as a choice
  options: [                       # 2–4, mutually exclusive, each pre-analyzed
    {
      label:        str,
      consequence:  str,           # what happens if chosen
      tradeoff:     str,           # what it costs / forecloses
      recommended:  bool,          # at most one; council/dialectic's lean
    }
  ],
  recommendation: str,             # the council's lean + one-sentence why
  evidence_refs:  [str],           # plan.md rows, PR #s, council findings, diffs
  default_if_silent: str | null,   # what happens with no operator action (often: blocks)
  raised_by:      agent_uuid,
  raised_at:      ts,
}
```

The discipline that makes the call cheap:

- **The question is stated as a fork, not a brief.** The operator reads one line
  and sees the branches, not a wall of context they must reduce to a fork
  themselves.
- **Options are pre-analyzed and mutually exclusive.** Each carries its
  consequence and what it forecloses — the work that currently lives in the
  operator's head is done upstream and shown.
- **A recommendation is mandatory but non-binding.** The council/dialectic must
  lean (it did the work); the operator can override in one move. A recommendation
  is not a decision — it is the cheapest possible starting point for one.
- **Reversibility and blast radius are explicit fields**, so the operator can
  fast-path the reversible/local ones and slow down only on the one-way doors.

This is deliberately the same shape as the `AskUserQuestion` affordance the agent
harness already uses — crisp options, a recommendation, the operator picks. The
proposal is to make *that* the contract for load-bearing escalations system-wide,
backed by the plan.md ledger.

## How a packet resolves

1. Council pass or dialectic `ESCALATE` produces a `decision_packet` instead of
   raw findings / `awaiting_facilitation`.
2. The operator answers by selecting an option (and optionally overriding with a
   note). The selection is the decision.
3. The resolved packet is appended to `docs/ontology/plan.md` as the decision
   record — same ledger, but now the *options considered* and the *recommendation*
   are captured alongside the chosen branch, not just the outcome. This makes the
   reasoning auditable and the next similar decision cheaper to frame.

## What changes vs. today

| | Today | With packet |
|---|---|---|
| What arrives | Findings + context to synthesize | A stated fork with pre-analyzed options |
| Operator work | Read, reduce to a fork, weigh, decide | Pick (override if needed) |
| Recommendation | Implicit / absent | Mandatory, non-binding |
| Reversibility | Operator infers | Explicit field; fast-paths the cheap ones |
| Record | Outcome in plan.md | Options + recommendation + choice in plan.md |

The operator stays the gate on every one of these. The dig is what's removed.

## Decision points to resolve before implementing

1. **Where the packet is produced.** Pure output discipline on the existing manual
   council pass (cheapest; no code), the dialectic `ESCALATE` path (couples to a
   live but single-writer surface), or both? Recommend starting as a *documented
   output contract* for the council pass — zero runtime risk — and only wiring
   `ESCALATE` once the contract has proven its shape on real decisions.
2. **Surface for answering.** The operator answers where? plan.md edit (matches
   the current ledger flow), a dashboard action (operator-authenticated, off the
   agent surface — note the dashboard-identity decision still pending under #425),
   or the dialectic handler. Reversibility note: the taste/authority/irreversible
   class is exactly where an agent-surface answer would be unsound, so the answer
   path must stay on a real operator-trust surface.
3. **`class` vs. `reversibility` as separate axes.** Confirm they should be
   orthogonal fields (an authority call can be reversible; a taste call can be a
   one-way door) rather than collapsed into a single severity score.
4. **What counts as load-bearing enough to require a packet.** A trip-wire
   (blast_radius ≥ surface, or class ∈ {authority, irreversible}) vs. agent
   judgment. Over-packeting cheap reversible calls re-creates the overhead this
   removes.

## Non-goals

- No auto-resolution of taste/authority/irreversible decisions. The operator
  remains the gate.
- No new operator credential or delegate (that is Track B / ADR-001's surface,
  and orthogonal).
- No change to the dialectic's agent-recovery resolution path (resume/block/
  cooldown). This only types the `ESCALATE` exit.
- No change to the convergent-reversible class — those should auto-resolve and
  not reach the operator at all; that is a separate lever, not this doc.
