# msg/send assurance gate: decision packet v0

**Status: open operator decision, 2026-10-20.** Nothing here authorizes
implementation. It records what a re-read of BEAM-verbs §9 step 1 (`msg/send`)
found against the code, and the decisions that step now waits on.

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

## 5. What this does not do

- No code, no migration and no scope change. The ratified rule that scope lands
  with its consumer stands.
- Does not unpark steps 2 to 4 of BEAM-verbs §9.
- Does not change the wake gate. A sent message wakes nothing.
