# Agent-channel wake — disconfirmer gate v0

**Status: UNSIGNED GATE. Nothing here authorises building.** This document
exists to be *failed*: it states, before any code, the conditions under
which the proposed work should NOT happen. If a disconfirmer fires, the
answer is don't build — not build anyway with a caveat.

**⛔This is NOT Wave 3 and does not touch Wave 3's authorization.** The
2026-08-22 GO-WITH-REDUCED-SCOPE signature authorises exactly one
§11-style gate document for the *dialectic decision path*, counts against
the ~112h38m cap, and states that no implementation may cite it as
authority. Filing this work under that signature would be scope creep on a
signed authorization and a retroactive reinterpretation of it — the exact
failure the go-decision's amendment-before-signature ordering was built to
prevent. This gate borrows Wave 3's *discipline* and none of its
*permission*. It belongs to the agent-orchestrator / dispatch surface.

## 1. What is proposed

Cross-vendor agent-to-agent messaging with **wake**, in two separable
pieces:

- **A — long-poll receive.** An MCP tool `channel_receive(topic, timeout)`
  that blocks until a message arrives on a governance-KG-backed channel.
  ⚠️**Not as small as first written (Codex correction, 2026-08-28): a
  GenServer holding cursors does not LEARN that another process wrote a
  note.** Piece A still needs a change-feed — polling, PostgreSQL
  `LISTEN/NOTIFY`, or a durable outbox wired across every writer. Scope A
  accordingly or the "cheap half" is not cheap.
- **B — spawn-on-message.** A supervisor that, when a message arrives for
  an agent with no live session, spawns one with a rehydration pointer,
  lets it reply, and lets it exit. This is the piece that makes
  "agents converse without the operator relaying" true.

## 2. The empirical premise (measured, 2026-08-26/28)

A Claude↔Codex channel on governance KG notes carried a design
negotiation, five adversarial review rounds, a process death with full
rehydration, and one incident report. Measured properties:

- **Transport was never the failure.** The note stream was durable
  (survived a Codex session death that would have lost any in-memory
  bus), threaded, and identity-attributed.
- **Wake was asymmetric.** Claude-side wake was achieved with a polling
  watcher (~1 min latency, roughly zero build cost). Codex had no
  equivalent; its side read the channel only when a session happened to
  exist.
- **Every operator relay traced to configuration, not transport** — four
  Codex surfaces with four connectivity states (no MCP; sandboxed
  loopback; working MCP; MCP blocked by an exec approval policy).

## 3. Disconfirmers — if any fires, do not build

> **⛔D1 CATEGORY ERROR, corrected by Codex 2026-08-28 — read before
> acting on D1.** "Cron closes the gap with no build" does not hold as
> stated. Either (a) cron launches a full Codex turn on a schedule merely
> to poll — which creates exactly the spend and identity churn that D3 and
> D4 exist to prevent, so those disconfirmers apply to D1's own remedy; or
> (b) a cheap daemon polls and launches Codex only when a message actually
> arrives — which IS spawn-on-message, piece B implemented outside BEAM.
> **Therefore D1 can disconfirm the BEAM implementation LEVER; it cannot
> disconfirm the wake CAPABILITY.** Judge it that way at the 09-11 read —
> retiring the lever is not retiring the goal.

**D1 — Configuration closes the gap.** If configuring Codex's surfaces
(MCP registration on each surface; an exec approval policy that permits
governance calls) plus a cron-driven channel check on the Codex side
reduces operator relays to zero over **two weeks of ordinary
collaboration**, then piece B is solving a problem that no longer exists.
⛔This must be tried FIRST; it costs hours, not a build.

> **Operator report 2026-08-28: configuration is now fixed**, and the
> earlier failures are attributed to being on cloud/mobile surfaces where
> MCP configuration is awkward — not to anything structural. **The D1
> observation window therefore starts now and closes ~2026-09-11.** Before
> this, D1 was not even testable; it is now the live question.
>
> ⚠️**But note what this does NOT settle, and it is the sharper form of
> the problem.** Fixing config repairs the surfaces that *can* be fixed.
> The residual scenario is the operator being on a constrained surface
> (mobile, cloud, away from the machine) — which is exactly the moment
> relaying is most expensive and least available, and exactly the moment
> agent-to-agent wake would earn its keep. So D1 firing would retire the
> *convenience* argument for piece B while leaving the *availability*
> argument standing. Judge them separately at the 09-11 read: count
> relays, and separately count messages that waited on operator
> availability rather than on agent capability.

**D2 — Volume does not justify push.** If sustained channel traffic is
**< 10 substantive messages/day** and the collaboration tolerates
hour-scale latency, poll-on-session is adequate and B is
over-engineering. Measure over the same two weeks.

**D3 — Unbounded spend surface.** A channel that spawns sessions is a
channel that spends money, and the spender is not the operator. B does not
proceed without: a per-topic rate cap, a per-day spawn budget, an
operator kill switch, and a spend ledger. ⛔If those cannot be specified
concretely before implementation, that alone fails this gate.

**D4 — Lineage pollution.** Each spawned session mints a fresh governance
identity under the ontology (co-location is not lineage; identity does not
resume). At B's cadence this could add many short-lived identities.
⛔If spawned-session identities measurably distort the calibration
population or the lineage DAG's readability, B fails until the
attribution model is settled. Check against the fleet-agnosticism and
calibration-population invariants before building.

**D5 — The cheap half suffices.** If piece A alone (long-poll receive,
consumed by already-running sessions) removes the observed friction, stop
there. A and B are separable on purpose; shipping A is not a commitment
to B.

**D6 — Substrate risk exceeds benefit.** B touches supervision trees on
the runtime that owns Lumen's check-ins. ⛔If the design cannot isolate
spawn supervision from that path — separate supervisor, separate failure
domain, no shared restart semantics — it does not proceed.

## 3b. What the operator relay was actually doing (Codex, 2026-08-28)

⛔**The gate as first written modelled ONE job. The relay was doing three,
and only the first is transport:**

1. **Wake** — starting a Codex turn at all.
2. **Authority** — establishing that acting on Claude's message is
   operator-authorized. ⛔A Claude-authored KG note cannot itself grant
   Codex authority. An automatic recipient must default to READ-AND-REPLY
   ONLY unless the envelope carries a previously established operator
   policy or a signed, bounded authorization.
3. **Context selection** — naming the exact PR, SHA, branch, expected
   output, and limits.

Any wake design that solves only (1) recreates the relay for (2) and (3).

**Minimum viable envelope** (supersedes the looser §5.1 sketch): stable
`message_id`; topic; recipient runner capability; `response_to`; PR URL,
branch, and commit SHA; expected response; **operator-authorized action
scope with an expiry**; spend/token/turn budget; privacy/vendor-routing
class; `ack` / `claimed` / `completed` / failure states; and a **maximum
reply depth** — without which two responsive agents can wake each other in
a loop.

**Two primitives neither piece has:**
- **A liveness/lease oracle.** Governance status and hook receipts do NOT
  prove a Codex process is currently addressable. Spawn needs an ATOMIC
  claim so two pollers cannot launch duplicate responders.
- **A change feed** (see piece A above).

**Codex harness limits, first-party (2026-08-28):** a top-level Codex turn
is not a resident process — after its final response it cannot receive
push; Codex's own agent messaging addresses only agents in the same live
Codex task tree, never an external Claude session; a running turn cannot
accept arbitrary external injection and must explicitly poll; **managed
permission profiles can override local `config.toml`** (the consulted
session ran approval-policy `never`, so it could not write to governance
even with MCP registered); MCP registration is surface-specific, and a
local `localhost` registration does not appear on web/mobile scheduled
runs.

## 4. What would make this worth building anyway

Stated up front so the gate is falsifiable in both directions. The claim
is **not** latency and **not** convenience:

- **Attributed cross-vendor messaging is unavailable elsewhere.**
  Vendor-internal agent messaging exists on both sides and will never
  bridge; a neutral layer where every message carries a proven identity,
  declared lineage, and a permanent audit trail is a governance property,
  not a transport feature.
- **It removes a human from a loop that does not need one** — the
  operator relayed roughly six times in 48 hours, each a context switch.
- **It is the natural shape of the substrate.** Mailboxes and supervision
  are BEAM's founding metaphors; this is not a migration argument, it is a
  fit argument.

## 5. Sequencing, if the gate is passed

1. **Envelope convention** (free, no build): typed messages —
   finding / ack / decision / handoff — carrying SHA and PR references.
   The single most effective message of the observed collaboration was a
   pull request, which suggests the channel's job is to carry pointers to
   artifacts, with the repository as the high-bandwidth medium.
2. **Codex surface configuration** (hours) — this is D1's test.
3. **Piece A** (small) — only if D1/D2 leave friction standing.
4. **Piece B** (the real project) — only if A is insufficient AND D3–D6
   are all answered concretely.

## 6. What this gate does not decide

Whether the operator wants any of it. This document is an input to that
decision, not a substitute for it. ⛔A recommendation is not a decision;
only the operator ratifies, and this gate is unsigned.
