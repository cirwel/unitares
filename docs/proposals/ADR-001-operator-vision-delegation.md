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

## Council review (2026-06-16, second pass)

A three-lane parallel adversarial pass (architect/security red-team,
code-reviewer, live-verifier) re-ran over this ADR and the Track A/B docs after
they landed in-repo. **The decision stands and is strengthened** — the "operator
token is not read-only" thesis is now triply grounded (see C4). But the pass
surfaced five forcing findings: one is premise-level on Track B, one is a live
standing hole independent of this work, and the Track A runbook's flag model is
factually wrong. All findings are code-grounded; lane transcripts are
ephemeral, so the load-bearing anchors are recorded here.

| # | Finding | Evidence | Forces |
|---|---|---|---|
| **C1** | **The "single read seam" is a fiction.** Redaction lives only in `query.py` (`list_agents`/`get_agent_metadata`). `observe(action='agent'/'similar'/'compare')`, `dialectic(get/list)`, and `knowledge` provenance emit **raw cross-agent UUIDs with no operator gate** — several `pre_onboard`, reachable unbound under strict. A delegate scoped to `query.py`'s helpers is incomplete and built on a false premise. | `observe/handlers.py:231,307,364,449,593`; `knowledge/handlers.py:337,351`; `dialectic/handlers.py:961-966,1025-1028` | Track B (new prereq) |
| **C2** | **Reusing the `operator_caller` boolean leaks resume credentials.** That flag also un-redacts `active_session_key` and `api_key`, not just UUID. Routing `caller_can_disclose()` through it hands a delegate the resume secrets. (Confirmed independently by two lanes.) | `query.py:197-201,236,263-269` | Track B design |
| **C3** | **"Disclosure-only" composes into resume in the default config, and the runbook's flag model is wrong.** Bare-UUID resume refuses only under `strict`, but `UNITARES_IDENTITY_STRICT` defaults to `log`. Two flags are conflated — `STRICT_IDENTITY_REQUIRED` (bool, default false, `identity_bootstrap.py`) vs `UNITARES_IDENTITY_STRICT` (3-mode, `governance_config.py:1019`) — and **neither governs disclosure**. `IPUA_PIN_CHECK_MODE` defaults to `strict`, not `log`. | `handlers.py:795-824`; `governance_config.py:1019,1055,1095` | Track A correction |
| **C4** | **The operator seam is write-capable, not read-only.** `resolve_operator_identity` mints a `caller_asserted` identity that bypasses every strict write gate (`phases.py` refuses only `server_inferred`). Confirms this ADR's thesis. | `operator.py:188-290`; `http_api.py:282-299`; `phases.py:300-342` | ADR confirmed |
| **C5** | **Blueprint's gate-axis threat model is mislabeled.** `handle_archive_agent` has no app-level operator gate (transport bearer only); `handle_operator_resume_agent` does not exist (real handler `handle_resume_agent`). Right conclusion, wrong mechanism. | `mutation.py:164-186`; `operations.py:39` | Track B blueprint |

### Non-forcing corrections folded forward

- **Disjoint-token-list gap is unguarded** — needs a fail-closed startup assertion; `wave3a_admin.py:50-60` keeps a duplicate parser.
- **REST mirror auto-shares the seam** — `list_agents`/`get_agent_metadata` fall through `execute_http_tool` to the same handlers, so any seam change is live on REST immediately; a delegate header must be captured in both `http_api.py:68-77` and `mcp_server.py:~989`, with REST rows in the test matrix. BEAM/Wave-3a has no identity middleware.
- **`_emit_audit` is mis-cited** — it lives in `src/identity/lineage_lifecycle.py` (keyword-only `details=`), not `identity/handlers.py`.
- **Divergent duplicate `_STRONG_IDENTITY_SOURCES`** in `services/identity_payloads.py` lacks `operator_token` — two sources of truth.
- **PR #610 presence-bypass trap** — the delegate gate must key on the *resolved binding*, never header/arg presence (the REST synthetic-CSID path).

### Confirmed clean
- `verify_agent_ownership` is pure binding-identity → `config(set)` / `dialectic(request)` genuinely untouched by the seam.
- v3.3-A `_build_public_payload` is write-time content redaction, not caller-keyed — cannot be widened by any caller scope (caveats: the node still carries raw `successor_id`; the `audit.r1_score_audit` read gate is unverified).

### Standing security finding (independent of this ADR)

`observe(action='agent', target_agent_id=<label>)` is an **open two-call
UUID-disclosure oracle today** (`observe/handlers.py:231`, label-resolvable, no
caller/operator check) — exactly the hijack the `list_agents` redaction was built
to stop. This is a live hole, not a Track B footnote, and should be tracked
separately.
