# Dialectic Resolution Receipt (`drr.v1`) — deployment-countersigned record, dormant

**Status:** wired and dormant. The code mints nothing on this deployment, and the
section "Why it stays dormant" names what has to exist before enabling it is an
honest act. Enabling is the operator's decision; this document is the packet.
**Code:** `src/dialectic_receipt.py` (mint, verify, canonical form),
`seal_resolution_for_persistence` in `src/mcp_handlers/dialectic/session.py`
(the terminal-write hook), `Resolution.receipt` in `src/dialectic_protocol.py`,
offline verifier `scripts/client/verify_resolution_receipt.py`,
tests `tests/test_dialectic_receipt.py`.
**Grounds in:** `docs/SCOPE_AND_THREAT_MODEL.md` ("The attestation half of the
same boundary"), the AIC prototype `agent-identity-credential-aic-v0.md`, the
identity plan's custody invariant (`docs/ontology/plan.md`, S19 B-strict), and
the federation trust-model commitment (per-principal governance, no shared
administrative root, cross-principal interaction via verifiable attestation).

## The gap

A dialectic resolution carries two attestations, one per party, each an
HMAC-SHA256 over the canonical payload keyed on that party's `api_key`. Inside
one operator's trust boundary that is sound, and it is unchanged here. It is
symmetric: whoever can verify can also forge, so the key can never be handed to
a second principal. The threat model states the consequence: a resolution record
is not independently verifiable by an operator who does not already trust its
issuer. It also says the decision upstream of any exchange work is *which
verification semantics a multi-principal deployment requires*, and names three
constructions: issuer non-repudiation, a transparency log, and a witness that
signs a receipt third parties verify.

## What is built

The third construction, as code that is inert until a key exists. When the
deployment holds an attestation key (`UNITARES_AIC_SIGNING_KEY`, the AIC
prototype's server-to-world key), the terminal `resolved` write countersigns the
stored resolution record with Ed25519 and attaches the receipt to the record
itself (`resolution_json`, so no migration and no new endpoint). A peer verifies
it with the deployment's public key and nothing else:

```bash
scripts/client/verify_resolution_receipt.py verify --record session.json \
    --jwks current-jwks.json --jwks retired-jwks.json --session-id <id> --issuer <name>
```

Design points that came out of review rather than the first draft:

- **Minted at acceptance, not at finalization.** `finalize_resolution` runs
  before the hard-limit gate, the self-review guard and execution; a receipt
  there would sign candidates governance then rejected. The receipt is attached
  only by the terminal `resolved` write (`save_session` and the explicit handler
  sites), never by a `failed` write, and it is cleared if the row is not written.
- **The digest names its fields.** The receipt carries `record_fields` and
  `record_sha256` over exactly those fields, so later schema additions do not
  invalidate earlier receipts, and a missing covered field is a mismatch. The
  encoding is RFC 8785 (JCS) compatible for this record shape.
- **The profile is enforced, not asserted.** The verifier rejects a correctly
  signed token whose `alg`, `stance`, `authorizes` or `status` claim is anything
  but `EdDSA`, `descriptive`, `[]`, `resolved`, and rejects a `kid` that does not
  match the supplied key. `authorizes: []` is a checked property.
- **The issuer is named.** `iss` reuses the lease plane's declared issuer
  (`UNITARES_LEASE_ATTESTATION_ISSUER`), so a receipt names the same deployment
  its lease attestations do; the verifier can pin it with `--issuer`.
- **Nothing changes without a key.** With no key the stored record has no
  `receipt` field, `Resolution.hash()` is the pre-receipt formula, and stored
  conditions are untouched. With a key, the record is written in the served
  shape (the read path's own normalization) so the stored JSON and every served
  copy are byte-identical; the parties' HMACs are invariant under that shape.

## What a valid receipt proves, and what it does not

| Proves | Does not prove |
|---|---|
| The holder of the private half of `kid` persisted, as `resolved`, a record whose covered fields had exactly these values | That either party intended the resolution |
| The session id and party identifiers the deployment associated with the record | That the parties' symmetric signatures are valid; a peer cannot check them |
| Whether two non-empty signature strings were stored (`both_signatures_present`) | That `signature_a` was keyed on a real `api_key`: an LLM-assisted session with no key on file signs with a fallback derived from the agent uuid and leaves `signature_b` empty |
| The `iat` the signer wrote | When the record was actually created; `iat` is a claim, unchecked, with no expiry and no revocation |

A named `reviewer_agent_id` can therefore appear on a record that reviewer never
signed. The verifier prints warnings for a single-signer record, for a session
id taken from the same document as the receipt, and for an unchecked issuer;
`verified: true` is a statement about the covered bytes, not about the
resolution's standing.

## Why it stays dormant

1. **Key custody.** The only custody available on this deployment is an
   environment variable in a LaunchAgent plist, and the identity plan rules that
   out for attestation in as many words: a plist secret under the same UID
   repeats the copyable-token bug at a different layer. Every co-tenant process
   could mint receipts indistinguishable from genuine ones. The AIC module's own
   docstring names a non-exportable keystore as the real-deployment option. It is
   not built. Enabling before it exists would make the receipt's central claim,
   "which deployment", mean "which UID on that machine".
2. **Key history.** `export_public_jwks` emits one key. The verifier accepts
   several JWKS documents so a retired key stays usable, but nothing publishes or
   retains history on the issuing side; a rotation without it turns every prior
   receipt into `unknown_kid`. The AIC's 24-hour hygiene argues for frequent
   rotation; a receipt is meant to be permanent. One seed serving both cadences
   needs history first.
3. **Revocation and back-dating.** With no expiry, no checked `iat` and no
   revocation, a leaked seed is retroactively total. The transparency-log
   construction the threat model names is what bounds that. This proposal builds
   the option that most depends on the option it did not build, and says so.
4. **No second principal.** Verifying this deployment's receipt on this
   operator's own hardware demonstrates the code, not the trust model, whose
   defining property is the absence of a shared root. The evidence would be a
   second operator with an independent channel to pin the key and a reason to
   check a record; that party does not exist and this document does not
   schedule one.
5. **The upstream decision is still the operator's.** The threat model parks
   the choice of verification semantics with the operator. Shipping one option,
   even dormant, creates gravity: it becomes the cheapest thing to extend. The
   review round made that objection and it is recorded here rather than argued
   away. What this code settles is only that the witness-receipt construction
   costs about four hundred lines and no new configuration surface.

The dormant-capability registry carries the matching `KEEP-DORMANT` entry with
the wake condition: non-exportable custody for the attestation key and a second
principal to pin it.

## Canonical form

`record_sha256` is SHA-256 over the UTF-8 JSON encoding of `{field:
record[field]}` for the fields listed in `record_fields`, keys sorted by code
point, separators `,` and `:` with no whitespace, non-ASCII emitted raw. For this
record shape (fixed ASCII keys; string, integer and list-of-string values; no
floats) that is byte-identical to RFC 8785. The signed message is the ASCII
bytes of `"drr.v1." + payload_b64url`; the receipt is `"drr.v1." +
payload_b64url + "." + signature_b64url`, unpadded base64url throughout. Claims:
`v`, `typ`, `alg`, `stance`, `authorizes`, `status`, `kid`, `iss`, `iat`,
`session_id`, `paused_agent_id`, `reviewer_agent_id`, `record_fields`,
`record_sha256`, `signature_version`, `both_signatures_present`.

## Failure behavior

A configured-but-unusable seed logs a WARNING and the resolution persists without
a receipt; persisting a resolution is a liveness path for a paused agent. If the
terminal row is not written (missing, or already terminal in another state) the
in-memory receipt is cleared. One edge remains: at the two LLM-assisted sites a
persistence exception after minting leaves the receipt on the in-memory object
until the session is next reloaded from PostgreSQL; the stored row, which is what
a peer is handed, never carries a receipt for a record that was not written.

## Non-goals

No party-held keys, no transparency log, no keystore, no `/.well-known`
endpoint, no change to the lease plane's single-issuer posture, no change to any
verdict, threshold or enforcement path, no change to the README's statement of
the deployed boundary. The governance-sensitivity inventory is not touched.
