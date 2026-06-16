# Track B — Implementation blueprint (`operator_delegate` disclosure scope)

- **Status:** Ready to apply against `CIRWEL/unitares` (this repo).
- **Prereq:** Track A enforced (`UNITARES_IDENTITY_STRICT=strict`,
  `UNITARES_SESSION_FINGERPRINT_CHECK=strict`). Do not ship Track B first.
- **Goal:** Grant a blessed agent session **read-only** UUID/lineage disclosure
  without granting the operator write/admin surface or any resume capability.

> Anchors marked **(verified)** were read directly from code. All anchors flagged
> "confirm-at-implementation" in the originating session have since been resolved
> against the repository and are now marked **(verified)**.

## Design invariant

Two capabilities must stay separate:

| Capability | Credential that may grant it | Must NOT be granted by delegate |
|---|---|---|
| Read-only identifier/lineage disclosure | full operator token **or** delegate grant | — |
| Write/admin, operator-token axis (`archive_agent`, `operator_resume_agent`, wave3a admin) | full operator token only | ✅ delegate rejected |
| Write/admin, ownership axis (`config(set)`, `dialectic(request)`) | owning agent's session (config-set also needs admin tag / 100+ updates) | ✅ delegate confers no ownership |
| Per-call identity resolution (resume) | full operator token (today) | ✅ delegate never a resolution source |

The delegate is **additive on the read path only**. Nothing already gated by the
full operator token loosens.

## Touch points

### 1. `src/mcp_handlers/identity/operator.py` — add a disclosure scope

Today: `is_operator_caller(signals: Optional[SessionSignals]) -> bool` **(verified)**
validates `X-Unitares-Operator` against `UNITARES_OPERATOR_TOKENS`
(`_OPERATOR_TOKENS_ENV`, **verified**; CSV→set helper, default deny, two-part
check).

Add a parallel, strictly-weaker check:

```python
_DELEGATE_TOKENS_ENV = "UNITARES_DISCLOSURE_DELEGATE_TOKENS"  # new, CSV allowlist

def is_disclosure_delegate_caller(signals: Optional[SessionSignals] = None) -> bool:
    """True if the request presents a valid *disclosure-only* delegate token.

    Same two-part shape as is_operator_caller (header present AND in allowlist),
    but against a SEPARATE allowlist. Default deny. Grants ONLY read-only
    identifier/lineage disclosure — never writes, never identity resolution.
    """
    # mirror is_operator_caller's signal extraction + allowlist compare,
    # keyed on _DELEGATE_TOKENS_ENV. A token may NOT appear in both lists
    # (validate disjoint at load; if it does, treat as operator, not delegate,
    # and log a config warning).

def caller_can_disclose(signals: Optional[SessionSignals] = None) -> bool:
    return is_operator_caller(signals) or is_disclosure_delegate_caller(signals)
```

(Option 2 / ontology-clean variant: replace the env allowlist with a per-session
grant store `{grant_id, agent_uuid, granted_by, scope, issued_at, expires_at}` and
have `is_disclosure_delegate_caller` resolve the bound agent's active grant. Start
with the env-token form behind the same `caller_can_disclose` seam so the call
sites below never change when you upgrade.)

### 2. `src/mcp_handlers/lifecycle/query.py` — widen the read gate only

Today **(verified)**: `_is_operator_request()` wraps `is_operator_caller()`; the
disclosure helpers `_visible_agent_identifier(...)` and
`_visible_related_agent_identifier(agent_id, caller_uuid, operator_caller)` gate on
`if operator_caller or agent_id == caller_uuid: return True`.

Change: feed these helpers from `caller_can_disclose()` instead of
`is_operator_caller()`. Rename the local for honesty:

```python
def _can_disclose_request() -> bool:
    from src.mcp_handlers.identity.operator import caller_can_disclose
    try:
        return caller_can_disclose()
    except Exception:
        return False
# pass this in where operator_caller= is currently sourced from _is_operator_request()
```

This is the ONLY behavioral widening. The gate logic itself is unchanged.

### 3. `src/mcp_handlers/updates/phases.py` — keep delegate OUT of resolution

The strong identity-source set `_STRONG_IDENTITY_SOURCES` containing
`"operator_token"` **(verified; set name `_STRONG_IDENTITY_SOURCES`, sibling to
`_MEDIUM_IDENTITY_SOURCES`)** — must **not** gain a delegate entry. The delegate
token is never an identity-resolution source; it cannot stand in as per-call
resume proof. Add a regression test asserting the delegate token does not resolve
identity (see test matrix).

### 4. Write/admin handlers — confirm they reject the delegate

The write surface is gated on **two distinct axes**, and the delegate token
satisfies **neither** — but the seam must not accidentally widen the operator-token
axis. Both groups verified in-repo:

**Axis 1 — operator-token, no ownership check.** These key on full-operator
presence (or its HTTP mirror) and do **no** per-agent ownership check, so they are
safe **iff** they keep gating on `is_operator_caller` / `_is_operator` (not
`caller_can_disclose`):

- `src/mcp_handlers/lifecycle/mutation.py` — `@mcp_tool("archive_agent", ...,
  register=False)` `handle_archive_agent`, *"No ownership check -- dashboard and
  operator agents need to archive"* **(verified)**.
- `src/mcp_handlers/lifecycle/operations.py` + `.../self_recovery.py` —
  `handle_operator_resume_agent`, *"No ownership check -- mirrors archive_agent
  pattern"* **(verified)**.
- `src/mcp_handlers/wave3a_admin.py` — gates via its own `_is_operator(request)`
  helper against `UNITARES_OPERATOR_TOKENS` (header `x-unitares-operator`, fresh
  allowlist read per request) **(verified)**. A delegate token is absent from
  that allowlist, so it 401s here with no code change.

**Axis 2 — session ownership (NOT the operator token).** These do **not** call
`is_operator_caller` at all; they gate on `verify_agent_ownership` against the
caller's bound UUID, so they are unaffected by the `caller_can_disclose` seam and
reject a delegate token automatically (a delegate confers no session ownership):

- `config(action='set')` → `handle_set_thresholds`
  (`src/mcp_handlers/admin/config.py`) — requires `verify_agent_ownership` **plus**
  an `admin` tag or 100+ updates (`is_admin or total_updates >= 100`) **(verified)**.
- `dialectic(action='request')` → `handle_request_dialectic_review`
  (`src/mcp_handlers/dialectic/handlers.py:532`) — gates on `verify_agent_ownership`
  (line 549) **(verified)**.

Action: keep Axis-1 handlers gating on `is_operator_caller` / `_is_operator`
(never `caller_can_disclose`); Axis-2 needs no change. Add an explicit test that a
delegate-only token is rejected by every handler in both groups.

### 5. Audit event

On any disclosure performed because `is_disclosure_delegate_caller` was true (i.e.
not full operator, not self), emit an audit event
`{event: "lineage_disclosed_via_delegate", grant_id|token_id, agent_uuid,
disclosed_agent_id}` mirroring the existing `_emit_audit` helper in
`src/mcp_handlers/identity/handlers.py` **(verified; `_emit_audit`, fail-soft,
used e.g. for `lineage_coincidental_rejected`)**. This restores attribution that a
shared bearer otherwise destroys.

### 6. Out of scope — do not touch

`_build_public_payload` (v3.3-A KG redaction,
`src/identity/trajectory_continuity.py`, **verified earlier**) stays
non-bypassable. The delegate scope is identifier/lineage disclosure only, never
KG payload widening.

## Test matrix

| Caller | Read UUID/lineage | archive/operator_resume/config-set | Resolves identity (resume) |
|---|---|---|---|
| no token | redacted | reject | no |
| self (`agent_id == caller_uuid`) | disclosed | n/a | own session |
| delegate token | **disclosed** | **reject** | **no** |
| full operator token | disclosed | allow | yes (unchanged) |
| token in both lists | treat as operator + config warning | allow | yes |

Plus: delegate token absent from the `phases.py` `_STRONG_IDENTITY_SOURCES` set;
disclosure via delegate emits the audit event; KG public payload unchanged for
delegate.

## Rollout

1. Land code behind an unset `UNITARES_DISCLOSURE_DELEGATE_TOKENS` (no-op until a
   token is configured — same default-deny posture as the operator token).
2. Confirm Track A strict flags are enforced.
3. Mint a delegate token, set it on exactly one trusted agent session, verify the
   test-matrix behavior live.
4. (Option 2) Replace the env allowlist with the per-session grant store behind
   the unchanged `caller_can_disclose` seam.
