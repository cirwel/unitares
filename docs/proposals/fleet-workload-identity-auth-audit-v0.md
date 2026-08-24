# Fleet Workload Identity and Tool-Call Audit - v0

**Created:** 2026-08-24  
**Status:** Draft v0.2 - threat model and pilot specification only. No live auth
or enforcement change is authorized by this document.  
**Council:** UNITARES dialectic `9b55c5144ee1b78a`, resolved `resume` on
2026-08-24 with the conditions folded below.  
**Independent review:** Claude Opus 5, 2026-08-24, verdict `REVISE`; Gate B
conditions and readiness findings are folded into v0.2 in section 13.

## 1. Decision summary

Permanent bearer tokens in launchd plists are the visible symptom of a missing
fleet workload-identity and credential-lifecycle contract. The first pilot will
replace the Lease Plane's static bearer check with a dual-mode verifier for a
bounded migration window, but only after the threat model, key custody,
issuance, audit, fallback, and acceptance contracts in this document are
approved.

The v0 direction is:

1. Each destination service owns issuance for its own audience. The Lease Plane
   pilot does not introduce a fleet-wide signing key or third-party credential
   runtime.
2. A caller authenticates over a peer-credentialed local channel from a
   distinct service account and proves possession of a distinct workload key
   held outside launchd configuration. Same-UID interpreted callers are not
   eligible for the pilot. The issuer maps that workload to an allowlist of
   scopes and mints a short-lived, one-use capability.
3. The destination service remains the authorization authority. Token claims
   do not bypass its exact route-to-scope map.
4. Security audit is separate from `audit.tool_usage`. The latter is currently
   asynchronous, fail-open telemetry and also feeds behavioral state; changing
   its population changes a governance sensor.
5. The automation ledger receives security-relevant transitions and bounded
   summaries, not one row for every high-frequency call.
6. Static bearer fallback is a measured migration mechanism with an enforced
   expiry, not an indefinite compatibility mode.

The pilot protects against plist disclosure and limits the value of a captured
request token. It does not claim to protect a fully compromised host or root
operator. Section 4 states the boundary precisely.

## 2. Existing state and integration boundaries

### 2.1 Current credentials

| Surface | Current credential | Current authorization behavior | v0 treatment |
|---|---|---|---|
| Lease Plane | `LEASE_PLANE_BEARER_TOKEN` | One static token for every route except force release | First pilot |
| Lease Plane force release | `LEASE_FORCE_RELEASE_TOKEN` | Separate elevated static token | Separate `lease.force_release` scope and workload allowlist |
| Agent Orchestrator | `AGENT_ORCHESTRATOR_BEARER_TOKEN` | One static token gates the whole router | No enforcement change before a later readiness review |
| Governance MCP/REST | `UNITARES_MCP_BEARER_TOKENS`, `UNITARES_HTTP_API_TOKEN`, OAuth/session paths | Transport and hosted-posture dependent | Inventory and specification only in this phase |
| Continuity | `UNITARES_CONTINUITY_TOKEN_SECRET` | Expiring HMAC ownership proof | Re-evaluate under this threat model; no automatic exemption |
| Wave 3a | `WAVE_3A_BEAM_TOKEN`, `WAVE_3A_PROBE_TOKEN` | Static outbound service tokens | Inventory and specification only in this phase |

Outbound callers currently read Lease Plane and Orchestrator bearer tokens from
environment variables. A verifier-only change would leave those permanent
credentials in the plists, so caller issuance and refresh are part of the same
migration unit.

### 2.2 Relationship to existing proposals

- [`cedar-delegation-authz-v0.md`](cedar-delegation-authz-v0.md) is the future
  policy-engine direction. This proposal supplies authenticated principals and
  capability scopes; it does not silently enable Cedar or duplicate its policy
  language.
- [`orchestrator-vouched-identity-v0.md`](orchestrator-vouched-identity-v0.md)
  covers headless-child identity. A vouched identity may later become an
  issuance input, but lineage alone is never authorization.
- [`track-a-strict-identity-hardening-runbook.md`](track-a-strict-identity-hardening-runbook.md)
  remains a prerequisite for delegation based on governance identity.
- [`s1-continuity-token-retirement.md`](../ontology/s1-continuity-token-retirement.md)
  narrowed continuity proofs to one hour and rotated their secret. That is
  useful precedent, not evidence that continuity already meets this proposal's
  custody, audience, replay, and revocation requirements.
- [`surface-lease-plane-phase-a-plan.md`](surface-lease-plane-phase-a-plan.md)
  remains the canonical execution history for the Lease Plane. This document
  does not reopen its lease semantics.

## 3. Assets, actors, and trust boundaries

### 3.1 Assets

- Lease ownership and exclusion state.
- Force-release authority.
- Workload identity private keys.
- Per-service token-signing private keys.
- Workload-to-scope policy and signer public-key registry.
- Capability tokens during their validity window.
- Security audit events, ordering, and retention.
- Static fallback credentials during migration.

### 3.2 Actors

- Operator: approves workload registrations, scopes, keys, exceptions, and
  emergency revocations.
- Issuing service: authenticates a workload and mints capabilities for its own
  audience.
- Calling workload: proves possession of its registered key and presents a
  capability.
- Protected service route: verifies the capability and independently enforces
  the route-to-scope map.
- Audit collector: accepts per-service security events and persists them to the
  canonical security store.
- Automation reporter: projects transitions and summaries for operator census.

### 3.3 Trust boundaries

1. launchd configuration to process environment. This boundary is currently
   crossed by permanent bearer material and is the immediate defect.
2. Calling process to local key custody. The caller may request a signature but
   must not receive exportable private-key bytes.
3. Calling workload to token-issuance endpoint. Issuance must authenticate the
   workload before evaluating requested scopes.
4. Issuer to protected route. The route trusts only an explicit issuer key,
   token type, audience, and algorithm.
5. Protected route to audit sink. Authorization evidence must survive process
   failure and must not silently disappear under backpressure.
6. Security audit to automation ledger. The ledger is a projection, not the
   security source of truth.

## 4. Threat boundary

### 4.1 In scope

- A plist, fleet-ops backup, process environment snapshot, or copied static
  configuration is disclosed.
- A short-lived capability is captured from memory, logs, transport, or an
  intermediary and replayed.
- A caller requests scopes, an audience, or a token type it is not allowed to
  use.
- A token for one service or route is presented to another.
- An attacker supplies an alternate JWT algorithm, unknown key, malformed
  claims, duplicate claims, or an ambiguous token type.
- A signer or workload key is compromised and must be rotated or revoked.
- A verifier has stale key or revocation state.
- Service clocks differ within or beyond the declared skew budget.
- The canonical audit store is slow, unavailable, full, or rejecting writes.
- A caller or operator accidentally downgrades from capability auth to static
  fallback.

### 4.2 Explicitly out of scope for the Lease Plane pilot

- A root compromise of the service host.
- Arbitrary code execution inside the Lease Plane process.
- A malicious operator with access to both key custody and policy state.
- Cross-host Linux/Pi key custody. The pilot is macOS-local; a portable custody
  mechanism requires a separate design.
- Replacing governance identity, Cedar authorization, or Orchestrator
  enforcement.

### 4.3 Honest residual risk and pilot eligibility

All current launchd jobs run as the same interactive user. Moving a key from a
plist into a login Keychain item, or allowing a signed helper to sign arbitrary
caller-supplied bytes, would relocate the secret and create a signing oracle. It
would not establish workload identity. Pinning a Keychain ACL to `python3`,
`beam.smp`, `node`, `bash`, or another shared interpreter is equally
non-discriminating.

The v0 pilot therefore requires distinct macOS service accounts for the Lease
Plane, its audit collector, and each participating workload. Issuance uses a
Unix-domain socket that authenticates the caller's peer UID before returning a
challenge. A private workload key is accessible only to its service account and
never appears in plist or environment data. A non-exportable System Keychain
item is preferred; a service-account-owned mode-0600 key file is admissible for
the custody spike, with the weaker extraction resistance recorded explicitly.

A signed helper is optional and admissible only if it authenticates each IPC
peer from the macOS audit token, checks an explicit designated requirement with
`SecCodeCheckValidity`, rejects unsigned/ad-hoc peers and `get-task-allow`, and
constructs the domain-separated message itself. It must never expose a generic
"sign these bytes" operation.

Gate A.5 is a throwaway custody spike with non-production keys and identities.
It must prove peer rejection and signing-oracle resistance before protocol code
begins. If distinct service accounts or authenticated helper peers cannot be
made operationally viable, the Lease Plane pilot is blocked.

## 5. Threats, controls, and falsifiers

| ID | Threat | Required control | Acceptance evidence |
|---|---|---|---|
| T1 | Plist or backup disclosure yields permanent access | No bearer, signing key, or workload private key in plist or environment after sunset | Secret-shape and key-name scan plus leaked-plist exercise cannot mint or call |
| T2 | Issuer compromise mints arbitrary fleet credentials | Per-service signer and audience; no fleet-wide signing key | Lease signer is rejected by every non-Lease audience |
| T3 | Captured token replay | One-use `jti`, durably consumed and committed before effect execution; failed attempts burn the token | Second use is rejected before route execution, including after failure, crash, and service restart |
| T4 | Confused deputy or overbroad caller | Workload-to-scope allowlist at issuance and exact route-to-scope check at use | Cross-scope and cross-route negative matrix |
| T5 | Algorithm, key, or token-type confusion | Fixed `EdDSA` allowlist, exact `typ`, explicit `kid`, duplicate-claim rejection | Alternate `alg`, missing/unknown `kid`, wrong `typ`, and duplicate claims fail closed |
| T6 | Stale verifier key or revocation cache | Same-process signer verification plus 5-second registry refresh and 30-second hard stale ceiling | Subject/key revocation and old-key retirement meet the declared time bound |
| T7 | Clock manipulation or drift | Issuer and verifier share one Lease Plane clock; v0 permits zero verifier skew | Boundary tests at exact `nbf` and `exp`; split issuance requires a new review |
| T8 | Audit loss, forgery, or reordering | Separate-UID collector authentication, durable intent, transactional terminal outbox, per-service sequence/hash, frequent external anchor | Kill/restart, forged submitter, duplicate delivery, truncation, and sink-outage tests |
| T9 | Static downgrade becomes permanent | Token-mode discrimination, no ephemeral-to-static retry, expiry and numeric sunset gates | Downgrade test, fallback-use alarm, and post-expiry rejection |
| T10 | Audit payload leaks credentials | Schema allowlist; no headers, raw args, token, signature, challenge, or private data | Adversarial redaction corpus and serialized-event inspection |

## 6. Lease Plane issuance and verification protocol

### 6.1 Ownership

The Lease Plane owns:

- its token-signing key;
- its issuer identifier and audience;
- the workload public-key registry;
- each workload's allowed Lease Plane scopes;
- challenge consumption and capability replay state;
- route-to-scope authorization; and
- Lease Plane auth/audit events.

No governance or Orchestrator identity automatically receives Lease Plane
authority. Registration is an explicit operator action.

### 6.2 Bootstrap identity

Each eligible calling workload runs under a distinct macOS service account and
receives a distinct Ed25519 key pair:

- Private key: inaccessible to other service UIDs, plist data, and process
  environment. Non-exportable System Keychain custody is preferred; any weaker
  custody requires an explicit spike result and operator acceptance.
- Public key: registered with Lease Plane under `workload_id` and `key_id`.
- Policy: exact list of scopes the workload may request.
- Rotation: at most 30 days between workload-key rotations.

The workload first requests a challenge over an issuance-only Unix-domain
socket. The Lease Plane authenticates the socket peer UID, binds the challenge
to the registered `(workload_id, key_id, peer_uid)`, applies a per-workload and
global issuance rate limit, and returns a random 256-bit nonce, audience,
issuance endpoint identifier, and a 30-second expiry.

The workload signs the UTF-8 RFC 8785 JSON Canonicalization Scheme bytes of an
object containing exactly these keys:

```text
protocol_version
workload_id
key_id
issuer_endpoint
audience
requested_scopes
challenge_nonce
challenge_expires_at
correlation_id
```

The signed message is:

```text
ASCII("UNITARES-WORKLOAD-ISSUANCE-V1") || 0x00 || JCS(request_object)
```

The request carries base64url-encoded canonical bytes and signature. The issuer
parses with duplicate-key rejection, re-encodes with RFC 8785, rejects any byte
difference, verifies the fixed domain prefix and signature, then compares every
field to the consumed challenge binding. `correlation_id` is a UUID, identifiers
are bounded ASCII, and scopes match `^[a-z][a-z0-9_.:-]{0,63}$`.

The issuer consumes each challenge exactly once, rejects unregistered scopes,
and only then mints a capability. A helper, if used, constructs this exact
object from its authenticated peer and exposes no generic signing API.

### 6.3 Capability format

The v0 wire format is a compact JWT signed with Ed25519:

- Header: `alg=EdDSA`, `typ=unitares-cap+jwt`, and registered `kid`.
- Claims: `iss`, `sub`, `aud`, `scope`, `iat`, `nbf`, `exp`, `jti`, `cid`,
  `token_use=capability`, and `version=1`.
- `sub` is the registered workload ID, never a caller-controlled display name.
- `scope` is a canonical single-space-delimited string. The issuer sorts and
  deduplicates requested scopes; the verifier treats them as a set.
- Default TTL: 60 seconds.
- Maximum TTL: 120 seconds.
- Clock-skew allowance: zero in v0 because issuer and verifier are the same
  Lease Plane process and use the same clock.
- Maximum ordinary stolen-token exposure: 120 seconds, before emergency key
  revocation is considered.

The verifier uses a parser that rejects duplicate JSON keys. It does not select
an algorithm from untrusted input: any value other than the exact fixed header
contract is rejected.

### 6.4 Replay handling

Each capability is one-use. Before route execution, Lease Plane atomically
inserts `(issuer, jti, exp, correlation_id)` into durable replay state and
commits that write. A duplicate insert rejects the request. Consumption is not
rolled back with the protected mutation: business rejection, timeout, crash, or
mutation rollback burns the token. The caller must obtain a new challenge and
capability before retrying.

Every mutating route must therefore define an operation idempotency key distinct
from token `jti`. A retry with a new capability and the same operation key must
return the prior committed result or execute the mutation at most once. No route
enters Gate C without that contract.

Replay state is retained until `exp`. The insert is fsync-durable before effect
execution, and a service or host restart must not make a consumed capability
reusable.

### 6.5 Exact route-to-scope map

| Method and route | Required scope |
|---|---|
| `POST /v1/lease/acquire` | `lease.acquire` |
| `POST /v1/lease/renew` | `lease.renew` |
| `POST /v1/lease/heartbeat` | `lease.renew` |
| `POST /v1/lease/release` | `lease.release` |
| `POST /v1/lease/force-release` | `lease.force_release` |
| `POST /v1/lease/handoff/offer` | `lease.handoff.offer` |
| `POST /v1/lease/handoff/accept` | `lease.handoff.accept` |
| `GET /v1/lease/status` | `lease.status.read` |
| `POST /v1/effects` | `effect.record` |
| `POST /v1/dialectic/session` | `dialectic.session.open` |
| `POST /v1/dialectic/phase` | `dialectic.phase.record` |
| `POST /v1/dialectic/reviewer` | `dialectic.reviewer.assign` |
| `POST /v1/dialectic/resolve` | `dialectic.resolve` |
| `GET /v1/dialectic/presence` | `dialectic.presence.read` |
| `GET /v1/health` | Unauthenticated minimal liveness only; no dependencies, identities, or policy detail |
| `GET /v1/health/detail` | `lease.health.read` |

Unknown routes and unknown scopes fail closed. `lease.force_release` is never
included in a general Lease Plane workload profile and requires a separately
registered operator workload key.

### 6.6 Key lifecycle

- Signing keys rotate at least every 30 days.
- Planned rotation publishes the new public key before activation. The old key
  becomes verify-only for 150 seconds, providing 30 seconds beyond the v0
  maximum token TTL, then is removed.
- Workload keys rotate at least every 30 days. Planned overlap is allowed only
  for a named workload and a maximum of 24 hours.
- Emergency signer, workload-key, or workload-subject revocation must become
  effective within 30 seconds of the operator action. The workload registry
  refreshes every 5 seconds and has a 30-second hard stale ceiling. Once the
  ceiling is exceeded, capability verification returns 503 until fresh policy
  state is available; stale state never remains valid without a bound.
- V0 does not promise individual-token revocation. If no key or subject is
  revoked, a specific already minted token can remain usable for at most 120
  seconds. Key or subject revocation is checked on every verification through
  the bounded registry state.
- Key IDs contain at least 128 bits of randomness and are never reused.
- Rotation and revocation each emit a security audit event and have a tested
  rollback procedure.
- Workload rotation enrolls the new public key through a proof signed by the
  current key plus explicit operator approval. Expiry warnings fire at 7 days,
  24 hours, and 1 hour. An expired workload key fails closed with a distinct
  internal reason code and never re-enables static fallback.

Signer private keys use the same non-exportability and signed-binary access
requirement as workload keys. Public verification keys and workload public keys
are not secrets, but updates to either registry require authenticated operator
authority and an audit event.

## 7. Authorization failure behavior

Static migration uses `Authorization: Bearer <token>`. Capabilities use the
distinct scheme `Authorization: UNITARES-Capability <jwt>`. Shape inspection or
JWT parse success never chooses the verifier. The raw HTTP boundary rejects
duplicate or comma-merged Authorization headers and forbids credentials in
query parameters or request bodies. Static comparison remains constant-time.

The verifier checks, in order:

1. Exactly one authorization credential is present.
2. The explicit authorization scheme selects exactly one verifier.
3. Header algorithm, type, key, and signature are valid.
4. Required claims exist exactly once and have the expected types.
5. Issuer, audience, token use, and version match exactly.
6. Time claims are within TTL and skew bounds.
7. The subject is an active registered workload.
8. Requested scope is present and still allowed for the workload.
9. `jti` consumption succeeds.
10. The required pre-execution security audit intent is durable.

Invalid or absent credentials return a generic 401. A valid capability lacking
route scope returns a generic 403. Detailed reason codes exist only in the
security audit. Audit, replay-store, registry-staleness, or issuance
infrastructure failures return a typed 503 and alert; they are never reported as
credential failures. A capability failure never retries against static auth.

## 8. Security-grade audit contract

### 8.1 Why `audit.tool_usage` is not presumed canonical

The current recorder explicitly promises never to break a tool call, schedules
database persistence asynchronously, and uses part of the stream as behavioral
and presence input. It is valuable telemetry, but those properties do not prove
durability, append integrity, or fail-closed authorization evidence. The pilot
must use a separate security stream unless `audit.tool_usage` is changed and
re-qualified without altering governance sensor meaning.

### 8.2 Canonical event schema

Every issuance attempt, verification decision, protected call, fallback use,
key change, revocation, and audit degradation emits a typed
`unitares.security_call_event.v1` record. Event types are
`authorization_denied`, `call_intent`, `call_terminal`, `outcome_unknown`,
`issuance`, `fallback`, `key_change`, `revocation`, and `audit_state`.
Allowlisted fields are:

```text
schema, event_id, service, service_instance, sequence, previous_hash,
event_hash, occurred_at, received_at, correlation_id, transport,
actor_workload_id, actor_governance_uuid, token_mode, token_issuer,
token_key_id, token_jti, audience, scope, method, route, tool, action,
authorization_decision, decision_reason_code, outcome, error_type,
latency_ms, audit_delivery_state
```

Raw authorization headers, tokens, signatures, challenges, request arguments,
response bodies, free text, and environment values are forbidden.

`correlation_id`, `event_id`, and `token_jti` are UUIDs. `route` is the static
router template, never a resolved target or query string. `decision_reason_code`,
`authorization_decision`, `outcome`, and `audit_delivery_state` are closed
enums. Every string has an explicit byte limit; invalid audit values fail schema
validation rather than being truncated into a misleading record.

### 8.3 Durability and integrity

- Before a protected mutation, the service writes and fsyncs `call_intent`.
  This event contains the authorization decision but no terminal outcome or
  latency.
- Successful DB-backed mutation and its `call_terminal` outbox row commit in
  one database transaction. Business rejection writes a terminal event in a
  separate short transaction after the protected mutation rolls back.
- A crash after intent but before terminal leaves an explicit orphan. A
  reconciler emits `outcome_unknown` or a state-derived terminal result; it
  never fabricates success.
- Authorization denial has one terminal `authorization_denied` event. Allowed
  mutations normally have one intent and one terminal event sharing a call ID.
- The audit collector runs under a different service UID. Its Unix socket
  verifies the submitting peer UID against the service registry. Forged or
  unregistered submitters are rejected and tested.
- Each service instance owns its mode-0600 fallback spool and monotonic
  sequence. Multiple services never append to one file. The caller workload UID
  cannot read or rewrite the Lease Plane spool.
- Events form a per-instance hash chain over canonical serialized fields. The
  chain root is acknowledged by the separate-UID collector at least every 60
  seconds. Roots exist in collector custody before local segments may be
  deleted.
- The collector writes idempotently by `event_id` into an append-only security
  table using an insert-only role distinct from the schema owner.
- Canonical database retention is 365 days. Acknowledged local spool segments
  are retained for 7 days.
- Collector duplicates are ignored by ID but counted. Sequence gaps, hash
  failures, forged submitters, or conflicting duplicates alert within 60
  seconds and have a 5-minute operator-response SLO during the pilot.

Spool size is not a fixed guess. Gate C measures calls by route and event bytes,
then provisions at least 24 hours of observed two-times-peak volume plus 100%
headroom. If the canonical sink is unavailable but the spool remains durable,
calls may continue and report `spooled`. If intent cannot append or reserved
terminal capacity is unavailable, protected mutations return 503 before
execution. Minimal `/v1/health` remains available without security detail.

### 8.4 Operational projection

The existing automation directory gains a sibling projection named
`security-transitions.jsonl` with schema
`unitares.security_transition_event.v1`. It contains:

- auth-mode transitions;
- first static fallback use after a quiet period;
- audit degraded and recovered transitions;
- key rotation and revocation outcomes;
- hourly bounded per-service success/failure/fallback summaries; and
- security-alert state changes.

It does not contain one event for every invocation. The canonical security store
retains per-call evidence; the operational projection keeps census useful
without turning a 120-second loop into hundreds of daily automation rows.

## 9. Static fallback migration and sunset

Before dual mode starts, every static Lease Plane token is rotated. The server
records a fixed fallback expiry no more than 14 calendar days after activation.
Static credentials remain distinct for ordinary and force-release paths.

An offline asymmetric recovery key is provisioned before dual mode. Its private
key is held by the operator outside launchd and service environments; only its
public key is pinned in Lease Plane. A manual, challenge-bound signature can
authorize only `auth.recover` or `lease.force_release`, creates a maximum
15-minute recovery window, is one-use, and must rotate after use. It cannot
authorize ordinary lease or dialectic routes. Break-glass use always creates a
separate operator-visible event and incident record.

Dual mode follows these rules:

- Credential format selects exactly one verifier. Capability failure does not
  fall through to static verification.
- Every static success records workload/caller attribution where known and emits
  a security event with `token_mode=static_fallback`.
- The first fallback use after 60 quiet minutes emits an operational transition.
- The migration owner is named before activation.
- An exception may extend one named caller for at most 48 hours. It requires an
  operator record, reason, owner, and new expiry. Exceptions do not extend the
  global static token.

Static auth may be disabled only after all gates pass:

1. Every inventoried caller and protected route class has used capability auth.
2. At least 50 successful capability calls are observed across the pilot, with
   at least 5 per inventoried caller and 3 per exercised route class.
3. The last 72 hours contain zero static successes outside an active exception.
4. Seven consecutive days have no unexplained issuance, verification, replay,
   key-refresh, or audit-delivery failure.
5. All negative, rotation, revocation, outage, and rollback acceptance tests
   pass against the release candidate.
6. Static credentials are rotated once more immediately before removal, then
   deleted from launchd plists and caller environments.

At day 14, static authentication fails closed unless a time-bounded operator
exception exists. Low traffic does not silently waive the call-count gates; the
pilot must run explicit canaries to obtain evidence.

## 10. Lease Plane pilot acceptance specification

### 10.1 Bootstrap and issuance

| ID | Required test |
|---|---|
| BOOT-01 | Registered workload signs a fresh challenge and receives only allowed scopes |
| BOOT-02 | Unknown workload, key, stale challenge, reused challenge, bad signature, and overbroad scope all fail closed |
| BOOT-03 | No private key or bearer appears in plist, environment, emitted event, or ordinary log |
| BOOT-04 | An unrelated same-user process cannot export or use the workload or signer key |
| BOOT-05 | Cross-audience issuance request is rejected before signing |
| BOOT-06 | An unregistered peer cannot obtain a signature through helper IPC; arbitrary-byte signing is unavailable |
| BOOT-07 | Shared interpreters and callers without the required service UID are ineligible and rejected |
| BOOT-08 | Non-canonical, duplicate-key, structurally ambiguous, wrong-domain, and altered RFC 8785 payloads fail verification |

### 10.2 Parsing and authorization

| ID | Required test |
|---|---|
| AUTH-01 | Every route in section 6.5 accepts its exact scope and rejects every other scope |
| AUTH-02 | Wrong issuer, audience, token use, version, type, algorithm, or key fails closed |
| AUTH-03 | Missing, duplicate, malformed, or incorrectly typed claims fail closed; scope order is normalized, not trusted |
| AUTH-04 | Unknown route and unknown scope fail closed |
| AUTH-05 | General Lease scopes cannot authorize force release; force-release scope cannot authorize ordinary routes by implication |
| AUTH-06 | Duplicate/comma-merged Authorization headers and query/body credentials fail before verifier selection |
| AUTH-07 | Explicit schemes select one verifier; malformed capability never invokes static comparison |
| AUTH-08 | A second-audience verifier stub rejects a valid Lease Plane signer and audience |

### 10.3 Time and replay

| ID | Required test |
|---|---|
| TIME-01 | Default and maximum TTL are enforced; caller cannot request more than 120 seconds |
| TIME-02 | `nbf` and `exp` use the shared Lease Plane clock with zero skew and reject at the exact boundaries |
| REPLAY-01 | Second presentation of one `jti` is rejected before route execution |
| REPLAY-02 | Restart between first and second presentation does not restore replayability |
| REPLAY-03 | Concurrent duplicate presentations result in exactly one authorized execution |
| REPLAY-04 | Business rejection, timeout, and mutation rollback burn the capability; retry requires a new capability |
| REPLAY-05 | New capability plus the same operation idempotency key cannot execute a mutation twice |

### 10.4 Rotation and revocation

| ID | Required test |
|---|---|
| KEY-01 | New signer activation and old-key verify-only overlap work for exactly the declared window |
| KEY-02 | Old signer is rejected after 150 seconds and cannot be reintroduced by stale cache |
| KEY-03 | Emergency signer and workload-key revocation take effect within 30 seconds |
| KEY-04 | Rotation rollback restores a known-good signer without accepting an unregistered key |
| KEY-05 | Subject/key revocation meets 30 seconds; absent revocation, individual-token exposure is bounded at 120 seconds |
| KEY-06 | Token minted immediately before rotation remains valid only through the 150-second verify-only overlap |
| KEY-07 | New-key enrollment requires old-key proof plus operator approval; expiry warns and then fails closed |

### 10.5 Audit and outage behavior

| ID | Required test |
|---|---|
| AUDIT-01 | Denials have one terminal event; allowed mutations have one durable intent and one terminal/outcome-unknown event |
| AUDIT-02 | No forbidden credential or free-text field survives serialization |
| AUDIT-03 | Duplicate delivery is idempotent; sequence gaps and conflicting duplicates alert |
| AUDIT-04 | Service kill/restart preserves acknowledged spool events and hash-chain continuity |
| AUDIT-05 | Database outage spools durably; spool write failure and full spool deny protected mutations |
| AUDIT-06 | Operational ledger emits transitions and hourly summaries without per-call high-frequency rows |
| AUDIT-07 | Unregistered UID and forged collector submissions are rejected without creating conflicting canonical events |
| AUDIT-08 | Collector acknowledges chain roots within 60 seconds; local truncation or rewrite is detected |
| AUDIT-09 | Mutation and terminal outbox commit atomically; crash gaps reconcile to outcome-unknown, never success |

### 10.6 Fallback and rollback

| ID | Required test |
|---|---|
| FALLBACK-01 | Static and capability formats select one verifier only; malformed capability never downgrades to static |
| FALLBACK-02 | Every static success is attributable or explicitly marked unknown and increments the sunset metric |
| FALLBACK-03 | Static auth fails closed at the fixed day-14 expiry without an active exception |
| FALLBACK-04 | A named exception affects only its caller and expires within 48 hours |
| ROLLBACK-01 | Capability mode can be disabled without losing newly rotated static separation during the pilot |
| ROLLBACK-02 | Rollback emits an event, preserves replay/audit evidence, and does not restore an expired static credential |
| RECOVERY-01 | Offline recovery signature opens only a one-use 15-minute recovery/force-release window |
| RECOVERY-02 | Recovery cannot authorize ordinary routes and requires key rotation plus incident closure after use |

### 10.7 Default-off invariance

| ID | Required test |
|---|---|
| FLAG-01 | With flags off, existing static auth responses and route behavior are unchanged |
| FLAG-02 | Fixed workload produces identical `audit.tool_usage`, behavioral-sensor, and presence inputs before and after merge |
| FLAG-03 | Enabling then disabling test-only capability mode leaves no active spool, replay, registry, projection, or background task |

### 10.8 Availability and capacity

| ID | Required test |
|---|---|
| AVAIL-01 | Issuer/key-custody outage during an active lease returns typed 503 and cannot silently downgrade |
| AVAIL-02 | Challenge flood is bounded by peer UID, 10 requests/second per workload, 200/second global, 8 outstanding per workload, and 1024 global |
| AVAIL-03 | At two-times projected peak heartbeat load, issuance p99 is at most 100 ms and causes zero renewal deadline misses |
| AVAIL-04 | Minimal unauthenticated health remains responsive during issuer, registry, replay-store, and audit degradation |
| CAP-01 | Measured event size and caller cadence prove replay, spool, collector, and 365-day retention capacity with required headroom |

## 11. Promotion gates

### Gate A - approve this design

The operator explicitly accepts or revises:

- per-service issuer ownership;
- distinct service-account bootstrap, peer-credentialed issuance, and the
  signing-oracle-resistant custody test;
- EdDSA JWT format;
- 60-second default and 120-second maximum TTL;
- zero v0 verifier skew;
- consume-before-execute durable replay state and operation idempotency;
- 30-second emergency revocation target;
- security-audit fail-closed behavior; and
- 14-day fallback window and numeric sunset gates.

### Gate A.5 - custody feasibility spike

Before protocol code, create only throwaway keys, non-production service UIDs,
an issuance socket, and any proposed helper. No live registry, plist, caller
environment, or service key changes are allowed. The spike must pass
`BOOT-04`, `BOOT-06`, and `BOOT-07` and record the exact custody mechanism and
code-signing/peer-credential evidence. Failure blocks the pilot.

### Gate B - prepare, do not deploy

After Gate A.5 passes, the first merged implementation is `FLAG-01..03`. Only
then may implementation add verifier/issuer abstractions, schemas, and the rest
of the acceptance harness behind default-off flags. No launchd plist, live key,
caller environment, or enforcement default changes are allowed in this gate.

### Gate C - Lease Plane pilot readiness review

A separate review examines test evidence, key operations, rollback, audit
qualification, the exact caller/route/cadence inventory, issuance availability,
and measured replay/spool/retention capacity before dual mode is enabled.

### Gate D - static sunset

Static auth is removed only when section 9's numeric gates pass. Removal includes
credential rotation and deletion from every plist and caller environment.

### Gate E - Orchestrator review

Agent Orchestrator receives a separate readiness review based on Lease Plane
evidence. No Orchestrator enforcement migration is implied by success of this
proposal or the Lease Plane pilot.

## 12. Explicit non-decisions

- Whether Cedar becomes the policy engine.
- Whether later services share an issuer implementation library.
- Linux/Pi workload-key custody.
- Full host-compromise resistance.
- Token exchange across service audiences.
- Operator or human login credentials.
- A retention period longer than the proposed 365-day pilot contract.

These require their own evidence and cannot be inferred from a successful Lease
Plane pilot.

## 13. Independent review disposition

Claude Opus 5 reviewed v0.1 on 2026-08-24 with tools, web access, and session
persistence disabled. The verdict was `REVISE`, not `BLOCK`: the direction was
sound, but the original contract would have certified incorrect behavior.

V0.2 resolves the Gate B blockers by:

- replacing same-UID helper trust with distinct service accounts,
  peer-credentialed issuance, explicit helper peer authentication, and a
  signing-oracle test;
- defining RFC 8785 canonical bytes and an issuance-proof domain prefix;
- choosing consume-before-execute replay semantics and requiring operation
  idempotency for safe remint/retry;
- splitting durable pre-execution intent from terminal/outcome-unknown audit and
  returning 503 for audit infrastructure faults;
- making off-state parity tests the first implementation artifact;
- selecting zero verifier skew, 120-second token exposure, 150-second key
  overlap, 5-second policy refresh, and a 30-second hard stale ceiling; and
- authorizing only a throwaway custody spike before protocol code.

The review's Gate C findings are now named requirements: issuance availability
and rate limits, collector authentication and external root custody, offline
asymmetric recovery, explicit credential schemes, bounded audit values,
workload re-enrollment, unauthenticated minimal liveness, capacity measurement,
and a complete caller inventory.
