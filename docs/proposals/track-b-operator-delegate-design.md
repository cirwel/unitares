# Track B — `operator_delegate` scoped disclosure design

- **Status:** Proposal (design-first; do not implement before Track A is enforced)
- **Target:** UNITARES governance system (`CIRWEL/unitares`)
- **Problem:** Let the operator reason over agent lineage **through their
  mediating agent**, without handing that agent the human-trust-root operator
  token. See ADR-001 for why reusing `UNITARES_OPERATOR_TOKENS` is unsafe.

## Requirement

Exactly one new capability is needed: **read-only disclosure of otherwise-redacted
agent identifiers and lineage edges** (`agent_id`, `agent_uuid`,
`parent_agent_id`, `identity_view`) to a specifically-blessed agent session.

Everything else the operator token carries — `archive_agent`,
`operator_resume_agent`, config-set, dialectic-request, Wave-3a admin/rollback,
and its role as a HIGH identity-resolution source in
`src/mcp_handlers/updates/phases.py` — must **not** be granted.

## The seam already exists

`src/mcp_handlers/lifecycle/query.py` already separates disclosure from operator
power: `_visible_agent_identifier(...)` and
`_visible_related_agent_identifier(..., operator_caller=...)` gate **only** whether
an identifier is shown vs. redacted. That read-only axis is distinct from the
WRITE surface in `wave3a_admin.py`. Track B carves the disclosure axis out as its
own scope rather than letting the env token grant both.

## Option 1 — Disclosure-scoped token class (minimal)

A second credential class that:

- `is_operator_caller`-style check recognizes **for disclosure only** — i.e. it
  satisfies the `operator_caller` branch in `query.py` `_visible_*` helpers.
- Is **rejected by every write handler** (`archive_agent`,
  `operator_resume_agent`, config-set, dialectic-request, `wave3a_admin`).
- Is **removed from / never added to** the HIGH `_STRONG_IDENTITY_SOURCES` set in
  `phases.py`, so it can never serve as per-call resume proof.

Pros: small change, reuses the existing disclosure gate. Cons: still a shared
bearer (unattributable, manual rotation); easy to misconfigure back into the
write path.

## Option 2 — First-class `operator_delegate` grant (recommended)

Make delegation an explicit, auditable identity object rather than a secret.

**Shape:**

```
operator_delegate {
  grant_id:      uuid,            # stable handle for audit
  agent_uuid:    uuid,            # the mediating agent's already-minted identity
  granted_by:    operator principal,
  scope:         ["lineage_disclosure"],   # closed set; disclosure only
  issued_at:     ts,
  expires_at:    ts,             # short, per-session
}
```

**Properties:**

- **Agent stays a governed peer.** Its identity remains minted/server-adjudicated;
  the grant is a separate, scoped object it *carries*, not a trust-root it *is*.
- **Attributable.** Every disclosure performed under a grant is logged with
  `grant_id` + `agent_uuid`, mirroring the existing audit-event pattern in
  `src/mcp_handlers/identity/handlers.py` (e.g. `lineage_coincidental_rejected`).
- **Expiring + per-session.** Rotation is automatic (grant TTL), not a manual env
  edit. A leaked grant dies on expiry and is scoped to disclosure only.
- **Enforced at the existing seam.** The `query.py` `_visible_*` helpers accept
  "operator_caller OR active disclosure grant for this caller"; write handlers and
  `phases.py` resolution sources are untouched, so no write/resume power leaks.

**Out of scope, explicitly:** the v3.3-A KG public-payload redaction
(`_build_public_payload` in `src/identity/trajectory_continuity.py`) is a
write-time content contract and must remain non-bypassable by any delegate scope.

## Decision points to resolve before implementing

1. **Issuance path.** How does the operator mint a grant for a session — dashboard
   action (operator-authenticated, non-agent surface) binding to the agent's
   UUID? CLI? This is the one step that must remain on a real operator-trust path.
2. **Scope vocabulary.** Start with a single `lineage_disclosure` scope; design
   the field as a set so future read-only scopes can be added without a new
   credential class.
3. **TTL + revocation.** Pick a default expiry (session-length) and a revoke path.
4. **Option 1 vs 2 sequencing.** Option 1 can ship as an interim if a disclosure
   need is urgent, but only *after* Track A, and with Option 2 as the committed
   end state — Option 1's shared-bearer downsides are exactly what Option 2 fixes.

## Non-goals

- No change to the resume/ownership model (PATH 0 continuity-token proof, S1-c).
- No new write or admin capability for agents.
- No widening of the KG public-payload contract.
