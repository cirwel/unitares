# Causal-only lineage declaration

Status: DRAFT (operator-decided 2026-06-14; pending council)
Author: agent ec46292a (lineage to 1b4172bb)
Related: PR #720 (archival liveness gate — the safety net under this change)

## Decision (REFINED post-council, operator-confirmed 2026-06-14)

The discriminator is **parent liveness**, not a blanket `new_session` ban. A
*live* parent means concurrent sibling (the archival cause); a *dead* parent
means genuine succession. Council live data: `new_session` edges confirm at 6%,
`subagent` at 33% — and `spawn_reason="explicit"` has been used 2 times ever
with a parent, so a pure "use explicit for handoffs" rule would silently kill
the real serial-handoff signal (operator close-and-continue workflow). So we
keep `new_session` edges flowing as *provisional* (R1 adjudicates the 6% real
ones) and gate only on liveness:

| spawn_reason | live parent | dead parent |
|---|---|---|
| `subagent`   | **allow** (dispatcher alive by design) | allow |
| `compaction` | **allow** (same live session continuing past a context boundary) | allow |
| `explicit`   | reject (concurrent sibling) | allow |
| `new_session`| **reject** (concurrent sibling — the archival cause) | allow as provisional → R1 |

**Liveness guard** uses the same `get_live_bindings()` signal as #720
(symmetric: #720 is the archival-time guard, this is the declaration-time one).
Mechanism is **record-then-demote** via the existing R2 pre-check / FSM (new
reason `lineage_coincidental_rejected`, mirroring `lineage_cross_role_rejected`)
so the audit trail survives. Rejected by council: write-time hard-drop (fights
the claim-based ontology, destroys audit) and `explicit`-as-handoff-carrier
(nobody sets it). Rejected: liveness-guarding `compaction` (its semantics
*require* a live parent).

## Why

The SessionStart nudge surfaced "the prior workspace session" as a lineage
candidate to *every* new session, and "continuing the workspace's work" reads
true for a concurrent session too. That minted false parent→child edges between
co-located, non-causally-related sessions (chain `1b4172bb → ad111882 →
d8c219dd`, none actually each other's children), which the (pre-#720) archival
path then acted on destructively. The onboarding nudge already warns "false
ancestry claims pollute the lineage DAG" — but defaults toward declaring it.

Lineage edges are most valuable to the trajectory-identity research (TIWD) when
each edge is a real causal event. Co-location inference is low-fidelity noise;
the system already treats these declarations as *provisional claims* (R2),
unconfirmed until R1 evaluation — so dropping auto-declaration discards noise,
not confirmed data.

## What consumes new_session lineage today (so we know the blast radius)

- `trajectory_identity.py` — seeds child trajectory **genesis** from the parent.
- `identity/trajectory_continuity.py` — registers the provisional lineage **claim** (R2).
- `identity/lineage_lifecycle.py` — `provisional → {confirmed,demoted,archived}` state machine; already has `lineage_cross_role_rejected`.
- `identity/provenance_chain.py`, `agent_fragmentation.py`, `provenance_context.py` — DAG / provenance views.
- `agent_lifecycle.py` (lineage_info), `stuck.py` (archival — fixed by #720).

## Proposed implementation (fit the existing framework)

1. **Server write point** — `ensure_agent_persisted` / the `_spawn` resolution in
   `src/mcp_handlers/identity/handlers.py` (~1207): stop the
   `("new_session" if _parent else None)` default from creating an edge. If the
   resolved `spawn_reason` is `new_session`, **do not persist `parent_agent_id`**
   (onboard fresh, `lineage_state: no_lineage_declared`). Only
   subagent/explicit/compaction persist the edge.
2. **Liveness guard** — before persisting an edge for explicit/compaction (NOT
   subagent), call `get_live_bindings(parent_id)`; if live, reject the edge
   (emit a rejection event, mirror `lineage_cross_role_rejected` — e.g.
   `lineage_coincidental_rejected`) and onboard fresh. Best-effort (DB error → []
   → allow, same posture as #720).
3. **Nudge surfaces** (stop steering clients to declare new_session lineage):
   - plugin SessionStart hook (`unitares-governance-plugin`)
   - `~/.claude/hooks/prompt-onboarding-nudge.sh` (local)
   - server onboard-response lineage hint
4. **Docs** (the coupled single-writer surface): `CLAUDE.md`/`AGENTS.md` shared
   contract (Minimal Agent Workflow + Identity rules), `docs/ontology/identity.md`,
   `commands/governance-start.md`, `skills/governance-lifecycle/SKILL.md`.

## Open questions for council

- Does dropping new_session edges break R2 evaluation or trajectory-genesis
  seeding for the *legitimate* serial-handoff case — and is that case now
  required to use `spawn_reason="explicit"`? Confirm the explicit path seeds
  genesis identically.
- Write-time hard-drop vs. record-then-immediately-demote via the existing
  state machine: which is truer to the framework and keeps audit trail?
- Liveness guard exemption for `subagent` — confirm the dispatcher-alive case
  is the only legitimate live-parent and nothing else relies on live-parent
  edges.
- Is there an existing "PR 3 consumer gating" effort this overlaps/collides with?
