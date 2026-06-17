# Operator Decision Packet — making load-bearing calls cheap to answer

- **Status:** Proposal (design-first; v1). No runtime code proposed in this doc.
  v1 corrects v0's factual claims about current wiring after a council pass
  (3 lenses, 2026-06-17) returned `withhold pending v1` — see the council-findings
  block at the end. The thesis survived all three lenses; only the scaffolding was
  wrong, and the corrections strengthen the "this doesn't exist yet" motivation.
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

- **Taste-level** — e.g. the three taste-level choices R1 v3.3 resolved on
  public-payload redaction / lifecycle states / scope (`r1-verify-lineage-claim.md`,
  2026-05-03). No correct answer; only operator preference.
- **Authority transition** — e.g. `seeded → earned` lineage is "a single explicit
  operator action" (`r1-verify-lineage-claim.md`). The operator *is* the trust
  root; no delegate can stand in.
- **Irreversible / high-blast-radius** — fleet-bricking territory (the S21 session
  resolution incident), destination commitments (the BEAM `A′` decision,
  `beam-footprint-roadmap-v0.md`).

These *should* bottleneck on the operator. The cost to attack is the per-decision
overhead, not the decision count.

## Where this plugs in (one live surface, two latent scaffolds)

Naming convention first, because the two mechanisms are distinct and v0 conflated
them: the **council** is a *human-facilitated* three-subagent pass that terminates
in **findings**; the **dialectic** is an *agent-driven* thesis/antithesis/synthesis
protocol that terminates in **resolutions**. The packet is a structured-output
contract that either can adopt — but its `recommendation` field is sourced from
whichever produced it, and the two are not interchangeable.

1. **The manual council pass (live, but non-code)** already does most of the
   upstream work — three subagents (`dialectic-knowledge-architect` +
   `code-reviewer` + `live-verifier`) converge on findings before a load-bearing
   change lands. Today the council terminates in *findings*; the operator reads
   them and synthesizes the call. The packet is the council's **output
   discipline**: findings → a structured decision the operator picks from. This is
   the only path that runs today, and it is operator-orchestrated, not code.

2. **The dialectic's `ESCALATE` action is a latent scaffold, not a working seam.**
   `ESCALATE` is defined in the `ResolutionAction` enum (`src/dialectic_protocol.py:195`)
   but **no resolution path ever emits it** — only `RESUME` is instantiated
   (`dialectic_protocol.py:881`). The operator-facing `awaiting_facilitation` state
   is reached *only* via the stuck-reviewer auto-timeout
   (`src/mcp_handlers/dialectic/auto_resolve.py:159-176`), never via `ESCALATE`.
   So the seam is **aspirational**: wiring it would be net-new code, not a re-route
   of something already flowing. v0 claimed ESCALATE "routes to awaiting_facilitation
   today" — that was false, and the gap is exactly the point: the typed-escalation
   mechanism this doc wants does not exist yet.

3. **A `design_review` session type also exists but is never instantiated.** The
   dialectic protocol has a `design_review` session type with long-lived timeouts
   (7-day antithesis / 30-day total, `dialectic_protocol.py:496-500`), which looks
   like a design-decision pathway — but `grep design_review` finds only two hits,
   both internal to `dialectic_protocol.py`; nothing in the handlers ever creates
   one, and it is still built around a `paused_agent_id` (agent-to-agent), not
   operator decisioning. So in *practice today* the dialectic does only
   agent-recovery. The corrected claim is "both ESCALATE and design_review are
   unwired scaffolds," not "the dialectic has no design pathway."

The packet is the common output contract the live council path can adopt now, and
the two latent scaffolds could adopt **if** they are wired — a code step this
design-first doc does not take.

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
- **A recommendation is mandatory but non-binding.** Whichever mechanism produced
  the packet must lean (it did the work); the operator can override in one move. A
  recommendation is not a decision — it is the cheapest possible starting point for
  one. (Source it from council *findings* or a dialectic *resolution*, not both
  silently merged — the field should name its provenance.)
- **Reversibility and blast radius are explicit fields**, so the operator can
  fast-path the reversible/local ones and slow down only on the one-way doors.

The shape is intentionally analogous to the harness-level `AskUserQuestion`
affordance — crisp options, a recommendation, the operator picks — but that is a
design *analogy*, not a dependency: `AskUserQuestion` is an agent-harness construct
and does not exist in the server codebase. The proposal is to make this the
contract for load-bearing escalations system-wide, backed by the plan.md ledger.

## How a packet resolves

1. The council pass produces a `decision_packet` instead of raw findings (live
   today). Once wired, the dialectic's `ESCALATE` exit would emit one instead of
   the current bare `awaiting_facilitation` flag (net-new code, not today).
2. The operator answers by selecting an option (and optionally overriding with a
   note). The selection is the decision.
3. The resolved packet is recorded in `docs/ontology/plan.md`. The ledger **already
   records operator decisions inline** with options and reasoning (the S1 / S15 /
   S19 rows carry "Operator decisions … stand: (a) … (b) …" prose). The packet is
   therefore a *structured tightening* of an existing discipline, not a new ledger:
   it pins the options-considered + recommendation + chosen branch in a consistent
   shape rather than free prose. Historical prose rows are left as-is (no backfill).
   Because plan.md is a single-writer surface (see the shared contract), packet
   resolutions append serially through the same operator-edit flow that governs the
   ledger today — concurrent packet writes are an operator-serialized edit, not a
   new concurrency model.

## What changes vs. today

| | Today | With packet |
|---|---|---|
| What arrives | Findings + context to synthesize | A stated fork with pre-analyzed options |
| Operator work | Read, reduce to a fork, weigh, decide | Pick (override if needed) |
| Recommendation | Implicit / absent | Mandatory, non-binding |
| Reversibility | Operator infers | Explicit field; fast-paths the cheap ones |
| Record | Inline prose in plan.md | Structured options + recommendation + choice in plan.md |

The operator stays the gate on every one of these. The dig is what's removed.

## Decision points to resolve before implementing

1. **Where the packet is produced.** Pure output discipline on the existing manual
   council pass (cheapest; no code, live today), or net-new code wiring the
   dialectic's unwired `ESCALATE` exit (couples to a single-writer surface)? Recommend
   starting as a *documented output contract* for the council pass — zero runtime
   risk — and only wiring `ESCALATE` (a from-scratch emit path, not a re-route)
   once the contract has proven its shape on real decisions.
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
5. **Track B coupling for agent-raised packets.** Does a packet whose `raised_by`
   agent must disclose cross-agent lineage to frame its options require a Track B
   `lineage_disclosure` grant (ADR-001), or is that strictly orthogonal? Resolve
   before any agent-raised packet path is built; council-pass packets (operator-
   orchestrated) sidestep it.

## Non-goals

- No auto-resolution of taste/authority/irreversible decisions. The operator
  remains the gate.
- No new operator credential or delegate (that is Track B / ADR-001's surface).
  One coupling to resolve, not assume away: a packet's `raised_by: agent_uuid`
  means an agent surfaced the decision — if that agent must also *disclose*
  cross-agent lineage to frame the options, it would need a Track B
  `lineage_disclosure` grant. Packets that only state a fork over the agent's own
  work need no such grant. Decision point 5 below pins which.
- No change to the dialectic's agent-recovery resolution path (resume/block/
  cooldown). The only dialectic touch is giving the *currently-unwired* `ESCALATE`
  exit a typed output — and that wiring is explicitly deferred (design point 1),
  not claimed as already done.
- No change to the convergent-reversible class — those should auto-resolve and
  not reach the operator at all; that is a separate lever, not this doc.

## Council finding (2026-06-17) — v0 → v1, "the seam already exists" was false

A three-lens council pass (`dialectic-knowledge-architect` + `code-reviewer` +
`live-verifier`, run in parallel) returned a unanimous `withhold pending v1`. The
thesis — package operator-essential decisions so the call is a pick, not a dig —
survived all three lenses; the forcing items were factual, and v1 applies them.
Notably, the council was itself convened and synthesized *as a decision packet*
(`dp-001`), dogfooding this contract.

Convergent forcing items, resolved in v1:

1. **The `ESCALATE` seam does not exist (all three lenses).** `ESCALATE` is a dead
   enum value (`dialectic_protocol.py:195`); no resolution path emits it — only
   `RESUME` is instantiated (`:881`). `awaiting_facilitation` is reached only via
   stuck-reviewer timeout (`auto_resolve.py:159-176`). v0's "routes to
   awaiting_facilitation today" was false. **Fixed:** §"Where this plugs in" now
   labels ESCALATE a latent, unwired scaffold; wiring it is net-new code.
2. **"dialectic only does agent-recovery" was overstated (knowledge-architect).** A
   `design_review` session type exists (`:496-500`) but is never instantiated
   (grep: 2 internal hits) and is still agent-to-agent. **Fixed:** added as a second
   latent scaffold, with the corrected claim.
3. **Misattributed citation (live-verifier).** The "three taste-level questions"
   are R1 v3.3 (2026-05-03), not S19 attestation. **Fixed:** re-cited to
   `r1-verify-lineage-claim.md`.
4. **"council" conflated two mechanisms (knowledge-architect).** Human-facilitated
   council (→ findings) vs. dialectic protocol (→ resolutions). **Fixed:** naming
   paragraph added; `recommendation` field must name its provenance.
5. **plan.md already records operator decisions inline (knowledge-architect).**
   **Fixed:** packet reframed as a structured tightening of an existing ledger
   discipline, with single-writer serialization noted and no backfill.
6. **Unbacked anchors (code-reviewer).** `AskUserQuestion` is harness-level, not in
   the server codebase; no schema/DB/handler for the packet. **Fixed:**
   `AskUserQuestion` demoted to an analogy; implementation cost called out as
   deferred, design-first.
7. **Track B / ADR-001 overlap (knowledge-architect).** `raised_by: agent_uuid`
   could require a `lineage_disclosure` grant. **Fixed:** added as decision point 5
   and qualified in non-goals rather than waved off as orthogonal.
