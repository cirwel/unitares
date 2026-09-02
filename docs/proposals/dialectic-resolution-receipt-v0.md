# Dialectic Resolution Receipt (`drr.v1`) — deployment-signed, peer-verifiable

**Status:** shipped off by default; the enabling decision is the operator's.
**Code:** `src/dialectic_receipt.py`, `Resolution.receipt` in `src/dialectic_protocol.py`,
offline verifier `scripts/client/verify_resolution_receipt.py`,
tests `tests/test_dialectic_receipt.py`.
**Grounds in:** `docs/SCOPE_AND_THREAT_MODEL.md` ("The attestation half of the
same boundary"), README "Local control and future federation", the AIC
prototype `agent-identity-credential-aic-v0.md`, and the federation
trust-model commitment (per-principal governance, no shared administrative
root, cross-principal interaction via verifiable attestation).

## The gap this closes

A dialectic resolution carries two attestations, one per party, each an
HMAC-SHA256 over the canonical payload keyed on that party's `api_key`. Inside
one operator's trust boundary that is sound and it is retained unchanged. But
it is symmetric: whoever can verify can also forge, so the key can never be
handed to a second principal. The threat model states the consequence plainly:
a resolution record is not today independently verifiable by an operator who
does not already trust its issuer. Every session run under the federation
mandate since 2026-08 has worked around that boundary; none had moved it.

## What ships

When the deployment has an attestation key configured, `finalize_resolution`
attaches a **receipt**: the deployment's Ed25519 signature over the stored
resolution record (the dict as persisted, minus the receipt itself). The
receipt lives in the record (`resolution_json`, and therefore every dialectic
tool response), so it needs no migration and no new endpoint. A peer verifies
it with the deployment's public key alone:

```bash
scripts/client/verify_resolution_receipt.py verify --record session.json --jwks deployment-jwks.json
```

No server, no database, no `api_key`. The verifier returns a stable reason
code on failure (`invalid_signature`, `record_mismatch`, `unknown_kid`,
`session_mismatch`, and so on) so a peer can say *why* a record did not check
out rather than only that it did not.

The key is the existing server-to-world attestation key
(`UNITARES_AIC_SIGNING_KEY`), the same seed and JWKS shape the Agent Identity
Credential prototype uses. The AIC proposal left an open question: is there a
concrete external verifier that would justify wiring asymmetric issuance? The
receipt is that first consumer, and it reuses the primitive rather than adding
a second key to manage.

## The semantics choice, stated as a choice

The threat model says the decision genuinely upstream of any exchange work is
*which verification semantics a multi-principal deployment requires*, and it
names three constructions: issuer non-repudiation, a transparency log
(inclusion and ordering without issuer authorship), and a witness that signs a
receipt third parties verify. This proposal builds the third. It is the
smallest construction that lets a peer verify a record's issuer, it matches
the trust bootstrap the two-governor federation trace already validated
(public-key pinning, no shared private root), and it forecloses nothing: a
transparency log could later include receipts, and party-held keys could later
countersign the same record.

It is not a claim that this is the *only* semantics a federation needs. That
question stays with the operator, which is why the feature is inert until the
operator sets the key.

## What a valid receipt proves, and what it does not

| Proves | Does not prove |
|---|---|
| This deployment (holder of the private half of `kid`) issued exactly this record | That either party intended the resolution |
| The record's action, conditions, root cause, reasoning, timestamp, both symmetric signatures and their version are the ones signed | That the symmetric signatures themselves are valid (a peer cannot check those and the receipt does not pretend to) |
| The session id and the two party identifiers the deployment associated with it | Anything about the parties' standing or authorization |
| Whether both symmetric signatures were present at finalization (`bilateral_symmetric`) | That the deployment's own governance was correct |

Party-level non-repudiation would need party-held asymmetric keys. That was
shelved on 2026-04-19 and this proposal does not reopen it.

## Canonical form

`record_sha256` is SHA-256 over the record encoded as JSON with the `receipt`
key removed, `conditions` reduced to stripped non-empty strings (exactly the
normalization the server's read path applies, so the raw database row and the
served response hash identically), keys sorted, compact separators, and
non-ASCII escaped. The signed message is the ASCII bytes of
`"drr.v1." + payload_b64url`. The claim set is `v`, `typ`, `alg`, `stance`
(`descriptive`), `authorizes` (always empty), `kid`, `iat`, `session_id`,
`paused_agent_id`, `reviewer_agent_id`, `record_sha256`, `signature_version`,
`bilateral_symmetric`. A receipt has no expiry: it attests a past event, and
key rotation is handled by `kid` (a peer keeps old public keys to verify old
receipts). `authorizes: []` is baked in so a receipt can never be read as a
credential, mirroring the AIC's self-describing authority boundary.

## Posture and failure behavior

- **Off by default.** No key, no receipt, no other change. Existing rows and
  legacy `signature_version` 1 rows decode exactly as before.
- **Never blocks a resolution.** A configured but unusable seed logs a WARNING
  and stores the resolution without a receipt; finalizing is a liveness path
  for a paused agent. An absent receipt therefore means "no usable
  attestation key at finalization", never a statement about the record.
- **Reload fidelity.** The session read path previously dropped
  `signature_version`, so every reloaded v2 resolution silently reported as
  legacy v1 and `hash()` drifted across a reload. It now carries
  `signature_version` and `receipt` through; this is a targeted fix on the
  same surface and is covered by tests.

## Operator decision packet

Nothing below has been done to the live deployment.

1. **Enable on the live server.** Generate a seed with
   `generate_signing_key_seed()` from `src/identity/agent_identity_credential.py`,
   add `UNITARES_AIC_SIGNING_KEY` to the governance plist, restart. From that
   point every finalized resolution carries a receipt. Cost: one secret to
   keep; a rotated seed changes `kid`, old receipts stay verifiable under the
   old public key.
2. **Publish the public key.** `verify_resolution_receipt.py export-jwks`
   prints the JWKS. Hand it to a peer out of band (pinning), or serve it from
   a `/.well-known` endpoint. The AIC docstring already marks that endpoint as
   a separate operator-gated step; this proposal does not add it.
3. **One key or two.** Receipts and identity attestations share the AIC key
   by design (one server-to-world attestation identity). If the operator wants
   receipts issued under a distinct key, that is a one-line change in
   `attach_receipt_if_configured` and a new catalogued flag.

The first real cross-principal verification is then a three-step act: enable,
export the JWKS to the Pi or to a peer operator, and have them run the
verifier against a Claude/Codex review resolution. That single verified
exchange is the fact the federation vocabulary has so far only asserted.

## Non-goals

No party-held keys, no transparency log, no change to the lease plane's
single-issuer posture, no `/.well-known` endpoint, no change to any verdict,
threshold, or enforcement path. The governance-sensitivity inventory is not
touched.
