# ADR-001 — Operator-vision delegation to agent sessions

- **Status:** Accepted (decision: do not enable as proposed; pursue Track A + Track B)
- **Date:** 2026-06-16
- **Scope:** UNITARES governance system (`CIRWEL/unitares`)
- **Deciders:** Operator + design council (security red-team, identity-ontology architect)
- **Supersedes:** an earlier informal "safe to enable now" recommendation, refuted by the council

## Context

In the CIRWEL single-operator deployment the human operator is **always mediated
by a conversational agent** — there is no routine direct human-to-server path. So
when the operator wants to reason over agent **lineage** (parent/child UUID edges)
that the governance server redacts, the natural-but-wrong move is to hand the
mediating agent session "operator-class" vision.

Agent identity/lineage is redacted from cross-agent reads. The redaction is
caller-keyed in `src/mcp_handlers/lifecycle/query.py`:

```
if operator_caller or agent_id == caller_uuid:
    return True   # disclose; else -> _public_agent_identifier(...), redacted
```

`operator_caller` is set by `is_operator_caller()`
(`src/mcp_handlers/identity/operator.py`), which validates the
`X-Unitares-Operator` header against the `UNITARES_OPERATOR_TOKENS` env allowlist
(default deny).

## Decision

**Do not grant agent sessions operator-class vision by reusing
`UNITARES_OPERATOR_TOKENS`.** The original "disclosure is decoupled from resume,
so this is safe now" thesis is **false in the deployment's own default
configuration.** Instead:

- **Track A (hardening, prerequisite):** flip `UNITARES_IDENTITY_STRICT` and
  `UNITARES_SESSION_FINGERPRINT_CHECK` to `strict`. See
  `track-a-strict-identity-hardening-runbook.md`.
- **Track B (the feature, design-first):** introduce a disclosure-only scope —
  ideally a first-class, per-session, expiring, audited `operator_delegate`
  grant — distinct from the operator write/admin token. See
  `track-b-operator-delegate-design.md`.
- **Interim:** the operator reads lineage through the dashboard (the non-agent,
  operator-driven surface); agents stay redacted.

## Why the thesis was refuted (verified against code)

1. **The operator token is not read-only.** It also authorizes un-owned WRITE
   actions. `src/mcp_handlers/lifecycle/operations.py` (operator resume):
   *"Lightweight resume handler for human operators (dashboard). No ownership
   check -- mirrors archive_agent pattern."* `tests/test_lifecycle_clobber.py`
   exercises `handle_operator_resume_agent` against an arbitrary
   `target_agent_id`. The same token class gates Wave-3a admin/rollback
   (`src/mcp_handlers/wave3a_admin.py`, `scripts/ops/wave-3a-rollback.sh`).

2. **The operator token is itself a resume/identity-resolution credential.**
   `src/mcp_handlers/updates/phases.py` lists `operator_token` as a HIGH
   identity-resolution source: *"X-Unitares-Operator bearer token, validated
   against the env allowlist on every call (#425) — per-call proof, stronger than
   the stable-header sources above."* So disclosure → resume is **one token, one
   hop**, not two decoupled capabilities.

3. **The gates that would re-couple safety are off in production.** Both
   `UNITARES_IDENTITY_STRICT` and `UNITARES_SESSION_FINGERPRINT_CHECK` default to
   `"log"` (`config/governance_config.py`). A live probe resolved a session via
   `"resolution_source": "ip_ua_fingerprint"`, auto-binding to an existing agent
   UUID with no ownership proof and no typed refusal — confirming non-strict mode.
   `config/governance_config.py` warns this path lets *"any caller who learns a
   UUID hijack the binding."* Operator vision is precisely a UUID-learning oracle,
   so it composes with this live hole even in a "read-only" framing.

4. **Ontology:** "operator-class" is a human-trust-root; an agent is a governed
   peer (`docs/ontology/identity.md` — identity is minted and server-adjudicated,
   not asserted; `lineage_coincidental_rejected`). Handing the trust-root bearer
   to a peer collapses the governed/governing distinction the system is built on.

## What stays out of scope of any new disclosure grant

The knowledge-graph public-payload redaction (v3.3-A,
`_build_public_payload` in `src/identity/trajectory_continuity.py`) is a
**write-time content contract**, not a caller-keyed access check, and must remain
non-bypassable by operator or delegate scopes. The full record already lives in
`audit.r1_score_audit`, reachable by `score_id` for operator-side queries.

## Consequences

- Lineage visibility for the operator is delivered through the dashboard until
  Track B ships; no agent receives operator-class vision in the interim.
- Track A has independent security value (closes the fingerprint-pin hole) and is
  a prerequisite for external pilots regardless of Track B.
- Track B replaces a shared, unattributable, non-expiring bearer token with a
  scoped, auditable, per-session delegation aligned to the identity ontology.

## Provenance

Decision recorded from a code-grounded design council over `CIRWEL/unitares`.
Cited code anchors were re-verified directly against the repository when these
docs were migrated in (the originating session read via `search_code` only).
Council members independently reached "needs-design-first" from the
composition-attack and category-error directions respectively.
