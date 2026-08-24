# Fleet Workload Identity and Tool-Call Audit - v0

**Created:** 2026-08-24  
**Status:** Draft v0.1 - threat model and pilot specification only. No live auth
or enforcement change is authorized by this document.  
**Council:** UNITARES dialectic `9b55c5144ee1b78a`, resolved `resume` on
2026-08-24 with the conditions folded below.

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
2. A caller authenticates to the issuer with a distinct workload key held
   outside launchd configuration. The issuer maps that workload to an allowlist
   of scopes and mints a short-lived, one-use capability.
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

### 4.3 Honest residual risk

All current launchd jobs run as the same interactive user. Moving a key from a
plist into a broadly accessible login Keychain item would relocate the secret,
not establish workload identity. The pilot therefore requires a non-exportable
Keychain key whose access control is pinned to a designated signed helper or
service binary. Acceptance test `BOOT-04` must demonstrate that an unrelated
same-user process cannot use that key. If macOS cannot enforce that condition
for the chosen integration, the pilot is blocked until jobs run under distinct
service accounts or another workload isolation mechanism is approved.

## 5. Threats, controls, and falsifiers

| ID | Threat | Required control | Acceptance evidence |
|---|---|---|---|
| T1 | Plist or backup disclosure yields permanent access | No bearer, signing key, or workload private key in plist or environment after sunset | Secret-shape and key-name scan plus leaked-plist exercise cannot mint or call |
| T2 | Issuer compromise mints arbitrary fleet credentials | Per-service signer and audience; no fleet-wide signing key | Lease signer is rejected by every non-Lease audience |
| T3 | Captured token replay | One-use `jti`, atomically consumed through durable state before effect | Second use is rejected before route execution, including after service restart |
| T4 | Confused deputy or overbroad caller | Workload-to-scope allowlist at issuance and exact route-to-scope check at use | Cross-scope and cross-route negative matrix |
| T5 | Algorithm, key, or token-type confusion | Fixed `EdDSA` allowlist, exact `typ`, explicit `kid`, duplicate-claim rejection | Alternate `alg`, missing/unknown `kid`, wrong `typ`, and duplicate claims fail closed |
| T6 | Stale verifier key or revocation cache | Versioned key set, bounded refresh, emergency invalidation, no unbounded stale-on-error | Revocation and old-key retirement meet the declared time bound |
| T7 | Clock manipulation or drift | Maximum TTL and +/-15 second skew bound; reject beyond either side | Boundary tests at `nbf`, `exp`, and skew edges |
| T8 | Audit loss or reordering | Durable append, per-service sequence, event hash chain, idempotent collector | Kill/restart, duplicate delivery, truncation, and sink-outage tests |
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

Each calling workload receives a distinct Ed25519 key pair:

- Private key: non-exportable macOS Keychain item, inaccessible to plist and
  process environment, constrained to a designated signed helper or service
  binary.
- Public key: registered with Lease Plane under `workload_id` and `key_id`.
- Policy: exact list of scopes the workload may request.
- Rotation: at most 30 days between workload-key rotations.

The workload first requests a challenge. The Lease Plane returns a random
256-bit nonce, audience, issuance endpoint identifier, and a 30-second expiry.
The workload signs a versioned canonical request containing:

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

The issuer consumes each challenge exactly once, verifies the signature against
the registered workload key, rejects unregistered scopes, and only then mints a
capability. Private key bytes never enter the calling process.

### 6.3 Capability format

The v0 wire format is a compact JWT signed with Ed25519:

- Header: `alg=EdDSA`, `typ=unitares-cap+jwt`, and registered `kid`.
- Claims: `iss`, `sub`, `aud`, `scope`, `iat`, `nbf`, `exp`, `jti`, `cid`,
  `token_use=capability`, and `version=1`.
- `sub` is the registered workload ID, never a caller-controlled display name.
- `scope` is a sorted, duplicate-free array drawn from the Lease Plane scope
  registry.
- Default TTL: 60 seconds.
- Maximum TTL: 120 seconds.
- Clock-skew allowance: 15 seconds in either direction.
- Maximum ordinary stolen-token exposure: 135 seconds, before emergency key
  revocation is considered.

The verifier uses a parser that rejects duplicate JSON keys. It does not select
an algorithm from untrusted input: any value other than the exact fixed header
contract is rejected.

### 6.4 Replay handling

Each capability is one-use. Before route execution, Lease Plane atomically
inserts `(issuer, jti, exp, correlation_id)` into durable replay state. A
duplicate insert rejects the request. Where a route mutation is backed by the
same database, capability consumption and the protected mutation occur in the
same transaction. If that cannot be achieved for a route, the route must define
an idempotency key and failure/retry semantics before entering the pilot.

Replay state is retained until `exp + 15 seconds`. A service restart must not
make a previously consumed capability reusable.

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
| `GET /v1/health` | `lease.health.read` |

Unknown routes and unknown scopes fail closed. `lease.force_release` is never
included in a general Lease Plane workload profile and requires a separately
registered operator workload key.

### 6.6 Key lifecycle

- Signing keys rotate at least every 30 days.
- Planned rotation publishes the new public key before activation. The old key
  becomes verify-only for 150 seconds, then is removed.
- Workload keys rotate at least every 30 days. Planned overlap is allowed only
  for a named workload and a maximum of 24 hours.
- Emergency signer or workload-key revocation must become effective within 30
  seconds of the operator action.
- If a durable token-level denylist is unavailable, incident documentation must
  state that already minted token exposure lasts up to 135 seconds.
- Key IDs contain at least 128 bits of randomness and are never reused.
- Rotation and revocation each emit a security audit event and have a tested
  rollback procedure.

Signer private keys use the same non-exportability and signed-binary access
requirement as workload keys. Public verification keys and workload public keys
are not secrets, but updates to either registry require authenticated operator
authority and an audit event.

## 7. Authorization failure behavior

The verifier checks, in order:

1. Exactly one authorization credential is present.
2. The credential is unambiguously static-migration or capability format.
3. Header algorithm, type, key, and signature are valid.
4. Required claims exist exactly once and have the expected types.
5. Issuer, audience, token use, and version match exactly.
6. Time claims are within TTL and skew bounds.
7. The subject is an active registered workload.
8. Requested scope is present and still allowed for the workload.
9. `jti` consumption succeeds.
10. The required security audit record is durable.

Any failure returns a typed 401 or 403 without disclosing which registered key,
subject, or policy entry exists. A capability failure never retries against the
static bearer path.

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
key change, revocation, and audit degradation emits a
`unitares.security_call_event.v1` record containing only allowlisted fields:

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

### 8.3 Durability and integrity

- The service appends the event to an fsync-backed, mode-0600 local spool before
  executing a protected mutation.
- Each service instance owns its spool and monotonic sequence. Multiple services
  never append concurrently to one file.
- Events form a per-instance hash chain over canonical serialized fields.
- The collector writes idempotently by `event_id` into an append-only security
  table using an insert-only database role.
- A daily chain root is retained outside the service spool so truncation is
  detectable.
- Canonical database retention is 365 days. Acknowledged local spool segments
  are retained for 7 days.
- Collector duplicates are ignored by ID but counted. Sequence gaps, hash
  failures, or conflicting duplicates alert immediately.

The spool has a 256 MiB hard limit. If the canonical sink is unavailable but the
spool remains durable, calls may continue and report `spooled`. If the spool
cannot append or is full, protected mutations fail closed. The pilot may allow
`GET /v1/health` and `GET /v1/lease/status` while audit is degraded only if their
events can still be durably spooled; otherwise they also fail closed.

### 8.4 Operational projection

The existing automation directory gains a sibling projection named
`tool-calls.jsonl` with schema `unitares.tool_call_event.v1`. It contains:

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

### 10.2 Parsing and authorization

| ID | Required test |
|---|---|
| AUTH-01 | Every route in section 6.5 accepts its exact scope and rejects every other scope |
| AUTH-02 | Wrong issuer, audience, token use, version, type, algorithm, or key fails closed |
| AUTH-03 | Missing, duplicate, malformed, incorrectly typed, or unsorted scope claims fail closed |
| AUTH-04 | Unknown route and unknown scope fail closed |
| AUTH-05 | General Lease scopes cannot authorize force release; force-release scope cannot authorize ordinary routes by implication |

### 10.3 Time and replay

| ID | Required test |
|---|---|
| TIME-01 | Default and maximum TTL are enforced; caller cannot request more than 120 seconds |
| TIME-02 | `nbf` and `exp` accept exactly within the 15-second skew budget and reject beyond it |
| REPLAY-01 | Second presentation of one `jti` is rejected before route execution |
| REPLAY-02 | Restart between first and second presentation does not restore replayability |
| REPLAY-03 | Concurrent duplicate presentations result in exactly one authorized execution |

### 10.4 Rotation and revocation

| ID | Required test |
|---|---|
| KEY-01 | New signer activation and old-key verify-only overlap work for exactly the declared window |
| KEY-02 | Old signer is rejected after 150 seconds and cannot be reintroduced by stale cache |
| KEY-03 | Emergency signer and workload-key revocation take effect within 30 seconds |
| KEY-04 | Rotation rollback restores a known-good signer without accepting an unregistered key |
| KEY-05 | Revocation behavior explicitly demonstrates token-level denial or the documented 135-second exposure bound |

### 10.5 Audit and outage behavior

| ID | Required test |
|---|---|
| AUDIT-01 | Every issuance and protected call has one canonical terminal event with correlation and auth provenance |
| AUDIT-02 | No forbidden credential or free-text field survives serialization |
| AUDIT-03 | Duplicate delivery is idempotent; sequence gaps and conflicting duplicates alert |
| AUDIT-04 | Service kill/restart preserves acknowledged spool events and hash-chain continuity |
| AUDIT-05 | Database outage spools durably; spool write failure and full spool deny protected mutations |
| AUDIT-06 | Operational ledger emits transitions and hourly summaries without per-call high-frequency rows |

### 10.6 Fallback and rollback

| ID | Required test |
|---|---|
| FALLBACK-01 | Static and capability formats select one verifier only; malformed capability never downgrades to static |
| FALLBACK-02 | Every static success is attributable or explicitly marked unknown and increments the sunset metric |
| FALLBACK-03 | Static auth fails closed at the fixed day-14 expiry without an active exception |
| FALLBACK-04 | A named exception affects only its caller and expires within 48 hours |
| ROLLBACK-01 | Capability mode can be disabled without losing newly rotated static separation during the pilot |
| ROLLBACK-02 | Rollback emits an event, preserves replay/audit evidence, and does not restore an expired static credential |

## 11. Promotion gates

### Gate A - approve this design

The operator explicitly accepts or revises:

- per-service issuer ownership;
- Keychain/signed-helper bootstrap and its same-user isolation test;
- EdDSA JWT format;
- 60-second default and 120-second maximum TTL;
- 15-second clock skew;
- one-use durable replay state;
- 30-second emergency revocation target;
- security-audit fail-closed behavior; and
- 14-day fallback window and numeric sunset gates.

### Gate B - prepare, do not deploy

After Gate A, implementation may add verifier/issuer abstractions, schemas, and
the acceptance harness behind default-off flags. No launchd plist, live key,
caller environment, or enforcement default changes in this gate.

### Gate C - Lease Plane pilot readiness review

A separate review examines test evidence, key operations, rollback, audit
qualification, and the exact caller inventory before dual mode is enabled.

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
