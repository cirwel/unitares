# Agent Identity Credential (AIC) — third-party-verifiable attestation (v0 prototype)

Status: prototype + design draft (not wired into the live identity path)
Author: Claude (session: identity-ux-fixes)
Date: 2026-06-24
Code: `src/identity/agent_identity_credential.py`, `tests/test_agent_identity_credential.py`
Grounds in: `docs/ontology/identity.md` (three stances, five-layer taxonomy),
`docs/ontology/plan.md` S19 (B-strict server-verifiable attestation), S1
continuity-token retirement, S19 copyable-bearer incident (Hermes/Sentinel).

## Why this, against the ontology

`identity.md` sorts every continuity mechanism into three stances:

- **Performative** — behave as if continuity holds, unverified. `continuity_token`
  *as a resume credential* is the named example, and it is marked "retire or
  repurpose." The S19 incident (a co-tenant process read Sentinel's anchor and
  replayed its token) is the concrete failure: possession ≠ identity.
- **Descriptive** — report what is actually continuous; stop faking the rest.
- **Inventive** — make claimed continuity *earn* its claim.

The AIC is a **Descriptive** primitive. It does not manufacture continuity or
confer authority; it lets the server make a *checkable statement* about standing
the agent has already accrued, and lets anyone verify that statement offline. It
passes the governing axiom — *"build nothing that appears more alive than it
is"* — because a signed, dated, authority-free attestation claims exactly as
much as it can back: "the issuer asserts these facts as of time T", nothing more.

It also answers the operator's S19 decision directly. plan.md (2026-04-25):

> B-strict must mean **server-verifiable or non-exportable attestation**, not
> "another copyable secret in a plist."

An Ed25519 keypair where the **server holds the private key and never exports
it**, and third parties verify with the **public** key, is precisely that — and
it is the asymmetric upgrade the symmetric-HMAC `continuity_token`
(issuer-verifiable-only) structurally cannot provide.

## What the prototype is — and is not

**Is:** a self-contained mint/verify/JWKS primitive.
- `mint_identity_attestation(...)` → `aic.v2.<payload>.<sig>` (Ed25519).
- `verify_identity_attestation(token, jwks=…|public_key=…, now=…, revoked_jti=…)`
  → claims dict or `None`. Offline; total (structure, prefix, signature,
  validity window, revocation).
- `export_public_jwks()` → JWKS document for a `/.well-known` endpoint.

**Claim set mirrors the five-layer taxonomy** so a verifier sees *which*
continuity is attested, not a flat "this is X":

| Claim | identity.md layer |
|---|---|
| `uuid` | registry / process-instance anchor |
| `structured_agent_id` | role layer (public handle; cosmetic, never proof) |
| `role_family`, `substrate_class` | role + substrate layers |
| `trust_tier`, `observation_count` | behavioral layer (accrued standing) |
| `lineage_state` | declared causal lineage (not claimed continuity) |

`stance="descriptive"`, `resume_capable=false`, `authorizes=[]` are baked into
the payload — the credential is self-describing about its own lack of authority —
and there is deliberately **no `sid`/session field**.

**Is NOT:**
- A resume/auth path. It does not touch identity resolution, the strict write
  gate, or `continuity_token`. A valid verify means *"the issuer authentically
  attested these claims"*, never *"the presenter is this agent."*
- A closure of the S19 agent→server resume-proof gap. That still needs the
  enrollment / process pre-registration plan.md discusses (a nonce minted at
  process-start, server-verified at resume). This module is the
  *verifiable-attestation building block* B-strict's "enrollment certificate"
  would stand on, demonstrated server→world.

## Two safety rules, enforced in code + tests

1. **Attestation, not bearer credential.** No `sid`; `resume_capable=false`;
   `authorizes=[]`; `is_resume_credential()` hard-returns `False`. Copying an AIC
   grants nothing — which is exactly why it is safe where `continuity_token` is
   not (S19). Tested: `test_attestation_carries_no_session_proof_and_no_authority`.
2. **Non-interchangeable envelope.** The `aic.v2.` prefix and `v2`/`opv:2`
   version are disjoint from the continuity_token's `v1.<…>` shape, and the
   signature is domain-separated (covers `aic.v2.<payload>`). A continuity_token
   string returns `None`. Tested: `test_verify_rejects_a_continuity_token_shaped_string`.

## Verification it works (15 tests, all green)

Round-trip; **third-party offline verify with only the published JWKS**; tamper
detection; wrong-key rejection; expiry + not-yet-valid windows; `jti` revocation;
kid stability; missing-key / empty-uuid misconfig; and the two safety rules above.

## If promoted past prototype (separate, operator-gated, writer-locked)

1. **Issuance.** Mint an AIC alongside `create_continuity_token` and surface it
   in onboard()/identity() `full` responses behind an opt-in (it is larger and
   most callers don't need it). Keep it strictly *additive*.
2. **Publication.** Serve `export_public_jwks()` at
   `/.well-known/unitares-identity-jwks`; add key rotation (multi-key JWKS, `kid`
   selection already supported).
3. **Revocation that bites.** Back `revoked_jti` with a published, signed
   revocation list — the targeted lever the TTL-only continuity_token lacks.
4. **Non-exportable key.** Move the signing seed from an env var to a keystore /
   HSM so the private key is genuinely non-exportable (the full S19 B-strict bar).
5. **Doctrine.** Update the `identity.md` identifier table + the AGENTS.md /
   CLAUDE.md shared contract to name the AIC as a *descriptive attestation*, with
   an explicit "never pass it as a resume proof" line — this is the coupled,
   cross-repo (unitares + gov-plugin) identity surface, so it needs single-writer
   coordination and an operator merge gate, not a drive-by.

## Open questions for the operator

- Is the demand here **server→world attestation** (this prototype), the
  **agent→server resume proof** (S19 B-strict enrollment, a different and larger
  build), or both? They are complementary but separately scoped.
- Is there a concrete external verifier today (second server, offline-verifying
  dashboard, agent-to-agent trust)? If not, asymmetric issuance is worth landing
  as a primitive but not worth wiring into the hot path yet.
