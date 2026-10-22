# msg/send assurance gate: decision packet v0

**Status: open operator decision, 2026-10-20; one recommendation, 2026-10-22
(§0).** Nothing here authorizes implementation. It records what a re-read of
BEAM-verbs §9 step 1 (`msg/send`) found against the code, and the decisions that
step now waits on.

## 0. Recommendation (2026-10-22)

Take Q1 (b): governance sends on the agent's behalf from the agent's own
caller-proven session. A three-seat council (dialectic, code review, live
verification) and an outside consult (consultation `dcb4bc98`) converged on it.

1. The agent calls a governance `msg` action in its own session. It holds no
   lease-plane bearer and no continuity token.
2. Governance derives the sender from that session, accepting only
   `caller_asserted` bindings, the same set #2681 issues credentials to. It
   mints a single-use `lat.v1` for that one request, as the presence site does,
   and forwards it to the lease plane with the server-held bearer. The lease
   plane is unchanged: `authorize_strict`, sender verified.
3. Inbox reads take the same route. A wrong session reading an inbox consumes
   at-most-once mail silently, so reads are never looser than sends.
4. An unknown, inferred or mismatched session is refused, not accepted with a
   label: an unknown session has no sender to label, and a stored wrong sender
   is a permanent false record. Each refusal writes an audit event carrying both
   claims, which is where a misattribution becomes visible.
5. Every send and refusal also writes a metadata-only audit event (sender,
   recipient, topic, time, how the sender was established), so the operator can
   see traffic without reading bodies. Today nothing is visible: bodies live
   only in `lease_plane.topic_messages`, for the recipient, for at most 7 days,
   and no dashboard view reads them. Message traffic stays out of `tool_usage`
   (`agent-message-transport-v0.md` §6).
6. Messages stay pointers to durable artifacts. Lease release, handoff, pause
   and resident commands keep their existing paths.

Why not the continuity token (Q1 a): every later record narrows it.
`docs/ontology/s1-continuity-token-retirement.md` labels it a possession proof
("performative, narrowed"); clients stopped caching it on 2026-04-21; a token
alone stopped resuming (#2465); #2681 (2026-10-06) made `client_session_id` a
credential issued only on proof; #2995 stops teaching the token on ordinary
calls. §7's sentence naming `recertify_strong_tier` predates #2681.

Why not agents posting to the lease plane directly: they would need
`LEASE_PLANE_BEARER_TOKEN`, which `http_auth.ex` applies to every route, including
lease routes whose identity binding runs in `:log`. The lease plane has no
session resolver, and `/v1/msg/send` takes the sender from the request body.

What it does not close: a session that picks up a neighbour's cache holds a
valid but wrong `client_session_id`, and the sender is then derived wrongly
with confidence. Catching that needs a second, independent claim: the harness
session id the SessionStart hook sees, compared with the one the session was
registered with (Q6 B).

Fits as written: `attestation-issuance-scope-v0.md` §4 (msg is its own mint
site) and §8f(4) (a fresh single-use attestation per send and retry). Changes:
§7's bar sentence, to name a caller-proven session. The operator's call: adding
the `msg` action (new actions stay reserved, 2026-10-15) and that reading of the
2026-08-29 "strong-assurance-gated" wording.

Sources: [`beam-verbs-as-contract-capabilities-v0.md`](beam-verbs-as-contract-capabilities-v0.md)
(§§2, 7, 8g, 9), [`attestation-issuance-scope-v0.md`](attestation-issuance-scope-v0.md)
(§4, ratified 2026-08-29), [`agent-message-transport-v0.md`](agent-message-transport-v0.md).

## 1. Finding: two different bars share one name

The RFC's §7 invariant says minting requires "strong, caller-proven assurance",
and names the bar as the one `recertify_strong_tier` sets on the attest route: a
caller-presented, unexpired cryptographic proof.

An ordinary MCP call bound by `client_session_id` is labelled
`proof_origin: caller_asserted`, which the update phases map to
`tier: strong, caller_proven: true`. That label describes a bearer session key
the client presents. It is not the attest route's bar: `recertify_strong_tier`
(`src/mcp_handlers/identity/session.py:446`) requires an HMAC continuity token
whose `aid` matches the holder, valid for at most one hour (`_CONTINUITY_TTL`,
`session.py:36`).

So "strong, caller-proven" means two things in the tree. An implementation that
gates the msg mint on the session label would satisfy the words of §7 and not
its intent. This packet treats the attest route's bar as the one §7 meant.

Consequence for interactive agents: the continuity token is returned by
`onboard` and `identity`, local caches deliberately do not persist it, and
passing it on ordinary calls is documented as a rebind-only use. Meeting the
literal bar means an agent passes `continuity_token` explicitly on each send and
refreshes it hourly.

## 2. Correction to the ratified scope document

`attestation-issuance-scope-v0.md` §4 says the presence site "keeps its two
paths", `{heartbeat, acquire}`. The presence helper also mints for
`POST /v1/lease/release` (`agent_presence_lease.py:503`). Its exemption covers
three bootstrap paths. The ratified model is unchanged; the enumerated set was
incomplete, and a scope declaration built from the document would break
presence release.

## 3. What step 1 would build (blueprint, for sizing)

- An exact-pair mint scope per site, replacing the `/v1/lease/` prefix check in
  `validate_path` (`src/lease_attestation.py:176`), with a dedicated msg site
  scoped to `{POST /v1/msg/send}`.
- A msg mint site gated on the decision in Q1, with the sender derived from the
  bound identity and caller-authored sender fields rejected.
- A `msg` router tool (`send`), an SDK `msg_send` method, and §6 dispatch
  recording that keeps capability calls out of `tool_usage`.
- §8g idempotency in the lease plane: a receipts table keyed by
  `(sender, operation_key)`, upserted atomically with the message insert. **This
  needs a migration** and a full lease-plane restart.

It touches identity and auth code, a migration slot, the consolidated tool
router and the lease-plane router, all single-writer surfaces.

## 4. Decisions waiting on the operator

**Q1. Which bar gates the msg mint?**
- (a) Literal: an explicit, unexpired continuity token on each send. This
  matches §7 and is awkward for interactive sessions.
- (b) Relaxed: the session's recorded credential-proof UUID equals the sender.
  This is friction-free and weaker than the attest route.
- (c) Middle: a short-lived send grant held server-side per session, created
  through a continuity check and bound to sender, session, the exact send scope
  and revocation state. This is a new delegated-session policy, not a reading
  of the existing invariant, and would need its own amendment.

Outside review (consultation `481e0117`, 2026-10-20) recommended keeping (a)
until the invariant is amended explicitly, and named (c) as the practical
middle. It warned against reinterpreting `caller_asserted` as recertification.

**Q2. Pilot or rollout?** The transport has carried no messages since it
shipped because no client exists. That is missing access, not measured absence
of demand. The review recommended a bounded pilot on the real delivery path,
with named workflows (blocker handoff, second-opinion request) and measures of
useful replies, interruptions and avoided operator relays, before any broad
agent-facing release.

**Q3. Retry and retention parameters.** Proposed: at most 2 attempts, retrying
only transport errors and 503 with identical bytes and a fresh attestation; an
operation deadline of about 15 minutes; receipts kept until the deadline plus
one hour. The review found these defensible only if an expired operation cannot
execute after its receipt is evicted and retries cannot extend message TTL.

**Q4. Stakes tier.** A message carries no authority, but it is agent-written
text placed into another agent's context, which makes it a prompt-injection
channel between peers whose identity makes the text more persuasive. The review
recommended `high` for the pilot unless recipients demonstrably treat envelopes
as untrusted input with preserved provenance.

**Q5. Deadline in the idempotency digest?** The review recommended yes: a
changed deadline on retry is a conflict, so a retry cannot silently extend an
operation's life.

**Q6. Where does the wake address live?** (added 2026-10-20) Waking or
resuming a specific agent needs its harness session id (the handle
`claude --resume` and `codex exec resume` take). Governance knows only the
agent uuid. Today the link exists only in the client cache
`~/.unitares/session-<harness_session_id>.json`, written after onboarding:
about 1,254 files, covering 41% of the last 30 days' Claude sessions, with no
retention. The server stores no harness session ids, and 1,248 of 1,252 cached
uuids map to exactly one session.

- (A) **Identity attribute:** a `harness_ref` written at mint on the identity
  record, unindexed, redacted for non-operator callers, and never an input to
  a resolver.
- (B) **Delivery address:** a wake-layer registry of
  `{uuid, harness, harness_session_id, registration_id, at, provenance, state}`,
  registered during onboarding, resolved uuid-to-address only. The identity
  record holds at most an opaque registration receipt.
- (C) **Client only:** fix the cache's coverage and retention and add a local
  uuid-to-session resolver. The server stores nothing.

A three-seat council and an outside consult (consultation `245f62c5`) both
leaned to (B). A resume handle locates a conversation, not an accountable
process, so on the identity record it invites the reverse lookups this system
bans. A shared handle would read as inferred lineage, and it would be exposed
through `agent(list)` and exports unless redacted field by field.

Design rules the review attached to (B):
- Register from the trusted SessionStart hook, which knows the harness session
  id before onboarding. A valid `client_session_id` proves authority over a
  uuid, not ownership of a conversation.
- Resolve uuid-to-address only. No reverse resolver, deduplication, inferred
  parentage or thread assignment. Several uuids may legitimately point at one
  conversation, so near-uniqueness must not become a constraint.
- An unregistered or unverifiable address is never woken. Fail visibly and
  never fall back to labels or neighbouring sessions.
- Keep the address out of the thread-sharing rollup, which groups agents by
  declared `thread_id` edges.
- The wake gate's spend caps, kill switch, and replay and concurrency checks
  apply at execution time. Two uuids pointing at one conversation must not
  cause concurrent resumes.

Counter-argument the review kept open: if the same agent-reachable credentials
control both stores, separating them is mostly cosmetic, and a narrowly
protected (A) may be safer than a poorly authenticated (B).

## 5. What this does not do

- No code, no migration and no scope change. The ratified rule that scope lands
  with its consumer stands.
- Does not unpark steps 2 to 4 of BEAM-verbs §9.
- Does not change the wake gate. A sent message wakes nothing.
