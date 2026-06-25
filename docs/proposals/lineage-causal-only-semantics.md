# Causal-only lineage declaration

Status: IMPLEMENTED (declaration-time liveness gate shipped; see "As-built" below)
Author: agent ec46292a (lineage to 1b4172bb)
Related: PR #720 (archival liveness gate — the safety net under this change)

> **As-built (2026-06-25).** The declaration-time liveness guard described
> here is live, not pending. It is the **liveness gate only** — the broader
> "blanket new_session ban" framing in the older "Proposed implementation"
> section below was superseded by the REFINED decision (gate on liveness, keep
> new_session edges flowing as provisional). What shipped:
>
> | Piece | Location |
> |---|---|
> | Declaration-time liveness gate | `src/mcp_handlers/identity/handlers.py` — `_r2_pre_check_and_declare` (runs before the cross-role check) |
> | `subagent` / `compaction` exemption | same function — `if spawn_reason not in ("subagent", "compaction")` |
> | Reject mechanism (clear + `lineage_coincidental_rejected` audit) | mirrors the `lineage_cross_role_rejected` path; fail-open on DB error (`get_live_bindings` → `[]` → allow), symmetric with #720 |
> | Tests | `tests/test_lineage_liveness_guard.py` (live→reject, dead→provisional, exempt→skip) |
> | Agent-facing nudge (stop steering to new_session lineage) | `src/tool_descriptions.py` |
> | Ontology | `docs/ontology/identity.md` §"Lineage is causal, not coincidental" |
>
> The open questions below were resolved by what shipped — answers inline in
> that section.

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

## Open questions for council — RESOLVED by what shipped

- **Does dropping new_session edges break R2 / trajectory-genesis seeding for
  the legitimate serial-handoff case?** Moot — we did **not** drop new_session
  edges. The refined decision gates on liveness only: a `new_session` (or
  `explicit`) declaration against a **dead** parent stays on the normal
  provisional → R1 path, so genesis seeding and R2 evaluation are unchanged for
  genuine serial handoffs. Only the **live-parent** (concurrent-sibling) case is
  rejected.
- **Write-time hard-drop vs. record-then-demote?** Resolved to **mirror the
  cross-role path**: `_r2_pre_check_and_declare` calls
  `clear_lineage_declaration(agent_uuid)` and emits a
  `lineage_coincidental_rejected` audit event. The durable audit trail survives
  in the event stream (same posture council accepted for
  `lineage_cross_role_rejected`); the row's `parent_agent_id`/`spawn_reason`
  columns are cleared in sync so the downstream FSM never reads back a rejected
  edge.
- **Liveness guard exemption for `subagent` (and `compaction`)?** Confirmed and
  shipped: both are exempt because their parent is legitimately still running
  (dispatcher alive / same live session past a context boundary). All other
  spawn reasons — `explicit`, `new_session`, and unset/unknown — are
  liveness-checked (conservative default).
- **Overlap with PR 3 consumer gating?** No collision observed; the guard sits
  at the declaration site (`_r2_pre_check_and_declare`) ahead of the existing
  cross-role pre-check, and the two rejection paths are independent
  (`rejected_coincidental` short-circuits before `rejected_cross_role`).
