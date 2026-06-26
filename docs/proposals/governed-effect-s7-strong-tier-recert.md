# Governed-Effect §7 — Strong-Tier Re-Certification (design v0.2, council-folded)

Extends the live §6 governance veto (#1073) with the contract §2/§7 gate:
**an `execute` agent_spawn commits only if the proposer's identity tier
re-verifies as `strong`** — without trusting a self-asserted tier.

## The problem (grounded, not rediscovered)

Tier is **not durable**. There is no `tier`/`proof_origin`/`assurance` column
in `core` or `audit`; `state_json` does not carry it. Tier is computed
per-request from the caller's *live* proof — `_tier_for_source(source)` at
`src/mcp_handlers/updates/phases.py:74`, gated by `proof_origin` at
`_compute_identity_assurance` (:126). The strict gate keys on
`caller_proven = (proof_origin == "caller_asserted")`.

**The trap:** `proof_origin == "server_inferred"` is *always* weak
(phases.py:149). A `client_session_id` resolved fresh at gov-mcp — which never
saw the proposer's original proof — is `server_inferred` → weak → would block
*every* effect. So the proof the effect carries must be one gov-mcp can verify
to the proposer's *real* tier without a fresh transport resolution.

## The proof: continuity_token (resolves Open-Q1)

The transport-robust proof is the **continuity_token** (`v1.<payload>.<sig>`,
HMAC-SHA256 over the secret in `_get_continuity_secret`). It is self-contained:
it carries `aid` (agent_uuid) and `exp`, and verifies cryptographically with no
transport context. `client_session_id` is rejected — it only re-certifies if
resolved fresh, which stamps `server_inferred` → weak (the trap).

A successfully-verified continuity_token resolves to source
`"continuity_token"`, which is in `_STRONG_IDENTITY_SOURCES`
(phases.py:40) → `_tier_for_source` returns **strong**. So:

> **verified, unexpired token whose `aid` == claimed proposer ⇒ strong; anything
> else ⇒ not strong ⇒ veto.**

## The verifier: reuse canonical crypto primitives (resolves Open-Q2)

Do **not** run the full transport identity-resolution pipeline (it resolves
fresh → `server_inferred` → weak). Reuse the existing single-sourced crypto
primitives in `src/mcp_handlers/identity/session.py`:

- `resolve_continuity_token(token)` — verifies HMAC **and `exp`** (freshness);
  returns the `sid` or `None`. Called with `model_type=None, user_agent=None`
  so the optional model-pin check is skipped (server-side re-verify has no
  transport context; the token's HMAC+exp+aid are sufficient).
- `extract_token_agent_uuid(token)` — verifies HMAC, returns `aid`.

Recert = the composition: token resolves (HMAC + fresh) **AND**
`extract_token_agent_uuid(token) == proposer_agent_uuid`. We require freshness
(via `resolve_continuity_token`'s `exp` check) — an irreversible spawn must not
re-certify on a stale token, unlike the resume-after-idle path that
`extract_token_agent_uuid` deliberately serves.

The gov-mcp process **already holds the secret** (it mints the tokens via
`onboard`), so no new secret/config is needed.

## On no-proof / unresolvable: FAIL CLOSED (resolves Open-Q3)

No token, malformed token, expired token, bad signature, or `aid` mismatch →
`tier_ok = false` → effect is **vetoed**. A weak/medium proposer is provably
blocked. This is the non-negotiable gate — it can never be a silent no-op.

### §7 dominates every allow path (council must-fix #1)

The §6 endpoint had **two** allow exits, and §6 deliberately fails *open* for an
unknown proposer (no governance state) via an early return — the bearer gates
access; §6's job is to stop a *known-flagged* agent. §7 is a different gate and
must apply on **both** exits, or the unknown-proposer path silently bypasses it.
So `tier_ok` is computed *before* the DB read, and the `row is None` branch
returns `vetoed = not tier_ok` (not an unconditional `false`). An unknown
proposer with **no token** is now blocked.

### Intent (council must-fix #2)

The composition is: **§7 gates identity strength, §6 gates behavioral
verdict.** A *strong, never-flagged* identity may spawn (§6 fails open, §7
passes); a *weak/unverified* identity may not, regardless of governance history.
This is deliberate — requiring prior governance history would block every
legitimate fresh proposer on an RCE surface that is already bearer-gated.

## Encapsulated gate (council must-fix #4)

The security invariant ("fresh HMAC AND `aid == proposer`") is single-sourced as
`recertify_strong_tier(token, proposer_uuid) -> bool` in
`identity/session.py`, unit-tested directly — **not** re-assembled inline at the
REST call site (where a future edit could drop `exp` by using `extract_*` alone,
or lose the `aid` binding by using `resolve_*` alone — note `resolve_*` returns
`sid`, not `aid`).

## Single proposer value — no veto/spawn TOCTOU (council must-fix #3)

The lease plane derives the proposer identity **once** (`env.proposer_agent_uuid`
from the envelope) and reuses that single value for (a) the veto's
`proposer_agent_uuid`, (b) the `aid` the token must match (checked at gov-mcp),
and (c) the committed spawn's lineage (`orchestrator_spec` `parent_agent_uuid`).
gov-mcp binds the token to that *same* claimed proposer (`aid == proposer`), and
the lease plane never re-resolves attribution from the token `aid` — so the
identity §6 judges, §7 certifies, and the spawn attributes to cannot diverge.

## Integration

### gov-mcp — `POST /v1/effect-veto` (`src/http_api.py:697`)

Extend the existing endpoint. Read an optional `proposer_continuity_token` from
the body. Compute `tier_ok`:

- token present + `resolve_continuity_token` non-None + `extract_token_agent_uuid
  == proposer_agent_uuid` → `tier = "strong"`, `tier_ok = true`.
- otherwise → `tier = "weak"|"unverified"`, `tier_ok = false`.

`vetoed = block_verdict OR block_action OR (not tier_ok)`. New response fields:
`tier`, `tier_ok`. The existing verdict/action veto is unchanged and still runs
(both gates compose; either trips the veto). Reason string names which gate
fired.

Backward-compat note: the endpoint previously had **no** tier gate. Adding a
`tier_ok=false`-by-default-when-token-absent gate is what makes §7 real — but it
means a caller that does not forward the token is now blocked. The lease plane
(the only caller) is updated in lockstep, so there is no external caller to
break. The recert applies **only to `execute`** effects (the lease plane only
calls the veto on execute agent_spawn today); record_only never reaches here.

### lease plane — carry + forward the token (transient, never stored)

The continuity_token is a **credential** (Invariant 7/1): forward-for-verify is
fine, storing is not.

- `governed_effect.ex validate/1` — the token can NOT live in `payload`
  (`@credential_key_substrings` at line 67 rejects it). Carry it in the
  **`proposer` object** as `proposer.continuity_token`; extract it into a
  transient `env.proposer_continuity_token` that is **never** written to any
  `audit_payload`/`execute_audit_payload` (those reference only named keys).
- `GovernanceVetoClient.check/1` — add `proposer_continuity_token` to the POST
  body. Nothing else changes; `:blocked`/`:error` still fail closed to
  `governance_blocked` (403), already wired in `spawn_and_record`.

The token is consumed (forwarded for verification) and dropped — it appears in
no persisted payload and no log line.

## The non-negotiable proof-test gate

Mirrors #1073's "no silent no-op" discipline:

- **Python (`tests/test_http_api_effect_veto.py`):** a strong proposer
  (verdict=safe/approve) **with a valid token** → `vetoed:false`; the **same
  proposer with NO token** → `vetoed:true` (tier gate fires); with an
  **expired/tampered token** → `vetoed:true`; with a **token whose `aid`
  mismatches** the proposer → `vetoed:true`. So the tier check provably blocks
  and provably allows — never a no-op.
- **Elixir (lease_plane test):** an `execute` agent_spawn whose envelope
  forwards a valid strong token commits (202); the same envelope with the token
  stripped is `governance_blocked` (403) and **nothing spawns**. Assert the
  token appears in no persisted `audit.events` payload.

## Deploy order

Same as #1073: **gov-mcp first** (it must understand `proposer_continuity_token`
and the new `tier_ok` semantics), **then lease plane** (which starts forwarding
the token). If lease plane forwards before gov-mcp is updated, gov-mcp ignores
the unknown field — but then `tier_ok` defaults false → fail-closed → blocked
until gov-mcp catches up. Safe-by-construction (fails closed, never open).

## Accepted residual risk + successor (council suggested)

§7 proves *identity strength*, not *per-effect authorization*. A captured
continuity_token replayed by a holder of the lease-plane bearer can spawn as the
strong identity for the token's life (~1h TTL). Bounding factors today: the
lease-plane bearer gate (`LEASE_PLANE_BEARER_TOKEN`), the §6 verdict gate, and
the ~1h `exp`. Idempotency does **not** bound replay (a new `effect_id` +
re-used token re-executes). The principled successor — **effect-binding**: the
proposer signs `(effect_id, payload_sha256)` at propose-time so the proof covers
*this* spawn — is filed for a later increment; the continuity_token cannot carry
it (pre-minted, doesn't cover the effect).

**Freshness cliff + sanctioned refresh:** the ~1h TTL is the token's
identity-rebind window, repurposed here as effect-freshness. A proposer whose
token minted >1h ago fails closed mid-session. The sanctioned escape is to
**re-mint via `onboard`/rebind before proposing** — NOT to drop the `exp` check.
`recertify_strong_tier` deliberately uses `resolve_continuity_token` (checks
`exp`) and never `extract_token_agent_uuid` alone, so this cliff cannot be
"fixed" by silently weakening the gate.
