# Thread-trajectory stitching — coherent metrics for orchestrated re-mints, without forging identity

**Status:** v0 proposal, for the governance-metrics / identity-rollup owners. Not a committed change.
**Author:** surfaced 2026-06-26 from a session-expiry investigation.

> **DEMOTED TO BACKSTOP (operator, 2026-06-26).** This proposal's framing — "the
> re-mint is honest; don't force continuity; stitch in metrics" — is partly
> *reversed* by the operator's preferred ontology: **Cylon-style instance-mortal /
> self-continuous**, where the thread *self* (identity + memory + trajectory) should
> persist across mortal instances rather than fragment. Under that frame the PRIMARY
> direction is the self actually carrying across a body-death (resurrection), capped
> today by the LLM provider's own session lifecycle (→ local models are the real
> end-to-end unlock). This stitch is retained as a **backstop**: even with a
> continuous self, genuine deaths (true long gaps) still occur and their segments
> should stay legible. Kept for the record; not the headline fix.
**Relationship:** extends `principal_rollup` (`src/services/principal_rollup.py`,
`docs/proposals/principal-rollup-v0.md`, council 2026-06-18) — same pattern, one new grouping signal.

## Problem (verified)

A dispatch_beam Discord thread resumes ONE governance identity per conversation via a
stable anchor: `UNITARES_CLIENT_SESSION_ID = agent:/thread-<id>` (`dispatch_beam
lib/dispatch/session.ex:460`, gated by `UNITARES_ORCHESTRATED=1`). That anchor is a
**client_session_id**, which rides the 24h **sliding** session TTL
(`SESSION_TTL_HOURS=24`; PG lookups filter `expires_at > now()`,
`db/mixins/session.py:101`). The anchor is **not** continuity_token-backed, and
dispatch never passes a token.

Consequence: a thread idle **>24h** loses its session binding; the next turn presents
the same `agent:/thread-<id>` key, finds no live binding, and **mints a fresh uuid**
on the auto-onboard path — labeled `spawn_reason="orchestrated_thread_anchor"`
(`src/mcp_handlers/identity/handlers.py:1733,1830`). So one multi-day conversation is
recorded under N agent uuids. This matches the observed
*"one conversation recorded under ≥3 uuids"* fragmentation.

## The framing that drives the fix: the re-mint is HONEST, not a bug

The substrate ontology is **honest non-continuity across restart**. A thread that goes
silent for days and resumes is, honestly, a *new process instance* — only the
conversation *context* survives (via dispatch's snapshot), not the agent. The
identity-honesty caveat is explicit and load-bearing: *"the harness provisions env, it
does not onboard or forge identity"* (`session.ex:456-457`). Therefore the wrong fixes
are the ones that make a new instance *claim* the old identity:

- **Inject a continuity_token (durable resume)** — makes a genuinely-new instance assert
  it is the prior one. Worst honesty fit; also hardest (dispatch never sees the agent's
  onboard response, so it cannot capture the token).
- **Longer TTL for `agent:/` sessions** — arbitrary, and still just postpones the honest
  re-mint.

The fragmentation is not an identity bug to suppress; it is honest reporting that the
**metrics layer** should accommodate. Roll the instances up at the *aggregation* layer,
leave the per-instance identities honest.

## Proposed fix: thread anchor as a principal-rollup grouping signal

`principal_rollup` already maps N agent instances → one logical "principal P"
(*"you are instance K of principal P"*), **advisory/display-only, NEVER a credential,
NEVER resolved-or-created as identity** (council 2026-06-18, unanimous). It currently
groups by **declared lineage** (`parent_agent_id`) and deliberately rejects
spoofable/coarse keys (IP:UA).

The gap: dispatch re-mints do **not** declare `parent_agent_id` (the anchor only sets a
client_session_id), so lineage-based rollup doesn't link them. Add one grouping signal:

> When `spawn_reason="orchestrated_thread_anchor"`, treat the anchor session key
> `agent:/thread-<id>` as a principal-grouping key, so all instances minted under one
> thread roll up to a single logical **thread-principal**.

The data exists and is queryable:
- `core.agent_sessions.session_key` is stored and indexed (`db/postgres/schema.sql:113,118`);
  it is the deterministic `agent:/thread-<id>`, identical across re-mints.
- `spawn_reason` is persisted on agent metadata (`agent_metadata_model.py:161`,
  `agent_metadata_persistence.py:135`).

So the rollup key is `(spawn_reason='orchestrated_thread_anchor', session_key)`, and the
sweeper (`principal_rollup_sweeper_task`) groups by it exactly as it groups by lineage today.

### Why this passes the rollup's "no spoofable keys" rule

`principal_rollup` rejects IP:UA because it is coarse and spoofable. The orchestrated
thread anchor is **not** that: it is an orchestrator-attested key, gated by
`UNITARES_ORCHESTRATED=1` — the *same* gate that already authorizes it to resume one
identity per conversation (`session.ex:447-454`). If the anchor is trustworthy enough to
resume identity, it is trustworthy enough to group a rollup. A leaked anchor on a
non-orchestrated session never sets `orchestrated_thread_anchor` (the marker is
fail-closed), so it cannot siphon unrelated subjects into one thread-principal.

## What it preserves / what it changes

- **Preserves:** every uuid stays an honest, distinct instance — per-resume
  non-continuity intact. No continuity_token, no TTL change, no harness identity forging.
- **Changes:** fleet/governance metrics and the onboard response gain a coherent
  *"instance K of thread-principal P"* view across the honest re-mints, so a multi-day
  conversation reads as one trajectory at the aggregation layer.

## Scope / owners / open questions

- This is metrics/rollup-layer work, not identity-resolution — same blast radius as the
  existing `principal_rollup` (advisory, fail-open, never a credential).
- Owners: governance-metrics / identity-rollup (the `principal_rollup` council).
- Open: should the thread-principal and a lineage-principal ever *merge* (an agent that
  is both a declared subagent and a thread anchor)? Default: thread anchor wins for
  orchestrated spawns; revisit if a real overlap appears.
- Out of scope: whether dispatch *should* instead carry a durable token — that's the
  less-honest path above and is explicitly not recommended here.
