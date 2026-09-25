# Federation trust decisions, 2026-09-25: multi-principal, custody, party HMAC

**Status:** decision record, 2026-09-25. It records three decisions and one
pointer. It implements nothing, changes no flag and enables nothing. D3a and D3b
are not scheduled. D5's implementation is a separate follow-up PR, tracked in
[#2449](https://github.com/cirwel/unitares/issues/2449).
**Basis, for every decision below:** Operator direction, 2026-09-25: "proceed
best for the future of the federation", given after an adversarial design review
of these decisions recommended the option recorded here. Merging remains the
operator's act, so the decision is reversible until merge.
**What that basis is and is not.** The direction is a delegation with one
criterion, in the form of the
[outcome-fixture precedent](../archive/outcome-fixture-conflation-decision-packet-v0.md):
the operator delegated these selections to the working agent, and under that
criterion the working agent selected the options below. The adversarial design
review was the authoring agent's own review, not an independent one, and it is
not recorded in the repository beyond this record. Nothing here claims the
operator weighed each option; the operator's ratification is the merge.
**Scope of "federation".** The word is used in
[`PRODUCT_DEFINITION.md`](../../PRODUCT_DEFINITION.md)'s sense: many runtimes
sharing one operator-controlled server and authority domain. D3a concerns trust
beyond that domain, which the product definition does not claim.
**Labels:** D3a, D3b, D4 and D5 follow the design review's numbering. They are
local to this record and unrelated to D1–D7 in
[`open-decisions-packet-v0.md`](open-decisions-packet-v0.md).

| ID | Question | Decision | What wakes or follows it |
|---|---|---|---|
| D3a | Schedule multi-principal trust work? | **Not scheduled** | A real outside party asks to verify a record from this deployment |
| D3b | Custody for the `drr.v1` receipt key | **Deferred behind D3a**; framing recorded for whoever wakes it | D3a waking |
| D4 | Peer-review path signs party A's slot with the caller's key | Fixed in [#2450](https://github.com/cirwel/unitares/pull/2450) (pointer below) | That PR's review and merge |
| D5 | Keep, restore or retire party-HMAC signatures | **Retire minting; keep history readable** | Implementation PR after D4 lands ([#2449](https://github.com/cirwel/unitares/issues/2449)) |

## D3a: multi-principal trust is not scheduled

**Decision.** No engineering work on multi-principal trust is scheduled.

**Why.** The trust model is defined by the absence of a shared administrative
root, and as of 2026-09-25 nothing on the other side of that line exists:

- **No second principal.** Only one operator runs this deployment. A second host
  run by the same operator is not a second principal, because it shares the
  administrative root, which is the one property the model turns on.
- **No external verifier.** As of 2026-09-25 the receipt verifier
  (`scripts/client/verify_resolution_receipt.py`) is not wired to any live
  surface, and `drr.v1` has issued no receipts. The dormant-capability registry
  records neither of its two issuance settings as set on the live process.

Engineering built ahead of a verifier can only be exercised on the operator's own
hardware, which demonstrates the code and not the trust model. That is reason 4
of the [`drr.v1` decision packet](dialectic-resolution-receipt-v0.md), and it
applies to the whole track, not only to receipts.

**Wake condition.** A real outside party, meaning another person or organisation
with its own administrative root, asks to verify a record from this deployment.

**Evidence path.** Outreach, not engineering. The next step that could produce
evidence is finding that party, not building for one. **Outreach is not
scheduled by this record either.** It is the operator's act, and this record
does not assign or plan it; nothing is in motion on this track.

**Where the wake would be noticed.** A request to verify a record would arrive
as an issue or discussion on the public repository, where the operator reads
it. **Backstop:** if no such request has arrived, this decision is re-read on
or after 2027-03-25; the date is a backstop for review, not a schedule for
work.

**What this answers.** The open question in the identity plan's S20 entry
([`plan.md`](../../ontology/plan.md), "is there a concrete external verifier
today that justifies wiring issuance into the hot path?"): no, not as of
2026-09-25. S20's other question, which direction of attestation is in demand,
is not answered here. It also turns reason 4 of the `drr.v1` packet ("that
party does not exist and this document does not schedule one") from an omission
into a recorded decision.

**What this does not do.** It retires nothing. No usage count enters it: what is
absent is a party, not calls to a capability. The `drr.v1` code stays wired and
dormant under its existing `KEEP-DORMANT` entry, and the AIC prototype is
unchanged. The validation cohort
([#1607](https://github.com/cirwel/unitares/issues/1607)) never depended on this
track and still does not. The [roadmap](../../ROADMAP.md) now carries the track
as not scheduled, with this wake condition.

## D3b: custody is deferred behind D3a

**Decision.** No custody work until D3a wakes. A custody design without a
verifier to hold it to has nothing to be tested against.

**Framing for whoever wakes it.** Recorded now so the next reader starts from
the argument rather than from scratch.

- **What kind of claim `drr.v1` makes.** It is a deployment-to-world
  attestation. The model is DKIM or a TLS server certificate: the signature says
  that a named deployment persisted this record, not which process inside it did.
  The claim is at deployment granularity.
- **Preferred option when woken.** A non-exportable key store that defends
  against off-host forgery, so that a copied seed cannot sign anywhere else. The
  receipt profile must then state plainly that "deployment" means a process
  running under the operator's account on the host, and claim nothing finer.
- **The strongest counter-argument.** In UNITARES the co-tenant processes on
  that host are the governed agents themselves. Under the preferred option, a
  party to a dialectic could have the store sign a receipt about its own
  dialectic. DKIM does not face this, because the mail it signs is not an
  attestation about its co-tenants.
- **What flips the choice.** Any receipt claim finer than deployment
  granularity, for example that the record was written by the dialectic path
  and not by a co-tenant, requires a signer under a separate account that
  governed agents cannot drive.
- **The key is shared with the AIC.** The `drr.v1` receipt signs with
  `UNITARES_AIC_SIGNING_KEY`, the same key the dormant Agent Identity Credential
  prototype would sign identity attestations with. A custody choice for receipts
  therefore binds the AIC as well.
- **One standard for both keys.** The lease plane's attestation key
  (`UNITARES_LEASE_ATTESTATION_SIGNING_KEY`, an Ed25519 seed supplied through
  deployment configuration) is live and already has deployment-granularity
  custody. Whatever custody standard is adopted for `drr.v1` must be applied to
  that key too, or the difference argued in the same decision. A dormant receipt
  cannot be held to a bar the live key is not.

**A seam with the packet.** Reason 1 of the `drr.v1` packet reads same-account
custody as disqualifying for attestation, following the identity plan's S19
caveat. S19 concerns agent-to-server proof, where a claim about one process is
exactly what same-account custody cannot support. The preferred option above
instead narrows the claim to what same-account custody can support. That is a
preference for whoever wakes D3b, not an amendment: the packet's wake criteria
stand until that decision is taken. In particular, citing this record does not
satisfy the registry's "non-exportable custody" criterion: whether a
same-account non-exportable store counts there is for the D3b decision itself
to settle.

## D5: retire party-HMAC minting, keep history readable

**Decision.** New dialectic resolutions stop receiving v2 party-HMAC signatures.
The following are kept:

- **History stays readable.** Existing rows are never stripped or backfilled.
  `describe_attestation` (`src/dialectic_protocol.py`) keeps classifying them,
  and historical signatures remain legacy and unverifiable by any peer.
- **The record shape stays fixed.** `signature_a`, `signature_b` and
  `signature_version` stay in the record, present and empty on new rows, so
  `Resolution.hash()` (served as `resolution_hash`) and the field set a `drr.v1`
  receipt covers do not change. A new row must read as `unsigned`.
- **`verify_signatures` and `compute_signature` stay** for reading history.

**Why.** The reasons follow from how the scheme is built, not from how often it
was used.

1. **It attests nothing beyond the server's own write.** The scheme is
   symmetric, and the server holds every key it verifies with. A party HMAC the
   server computes with a key the server stores says only that the server wrote
   this row, and the attributed row already says that. It cannot be party-level
   evidence even in principle.
2. **Restoring it would revive a retired credential.** API keys were deprecated
   as an authentication mechanism: UUID session binding is authentication on
   the handler path. `src/mcp_handlers/tool_stability.py` records the
   `get_agent_api_key` alias with the migration note "API keys deprecated -
   UUID is now auth", dated on or before 2026-01-13, the floor that file gives
   for the repository's history. Issuing keys again to feed the HMAC would bring
   back a credential the identity model has deprecated. Deprecated is not
   removed: some legacy paths still generate a key when metadata is created,
   and one still verifies ownership with it. So the implementation must stop
   minting in `finalize_resolution` itself, not rely on keys being absent.

The 2026-09-08 measurement in the `describe_attestation` docstring (no agent
minted with an API key since 2026-01-29) is telemetry, not a reason for this
decision. The decision would be the same if every agent held a key.

**The goal is kept.** Party-level non-repudiation is still a goal. Its lever is
asymmetric keys held by the agents, the DPoP-style work shelved on 2026-04-19
(see [`UNIFIED_ARCHITECTURE.md`](../../UNIFIED_ARCHITECTURE.md)). This retires a
lever that could not reach the goal. It does not retire the goal.

**Implementation.** A separate follow-up PR, after the D4 fix lands, because
both change the same call into `finalize_resolution`. It is tracked in
[#2449](https://github.com/cirwel/unitares/issues/2449). The constraints above
bind that PR. Which `signature_version` value new rows carry is left to it,
provided a new row reads as `unsigned` and no field is added or removed.

## D4: pointer

On `master` as of 2026-09-25, on the peer-review synthesis path, a paused agent
with no key on file gets `signature_a` keyed on the `api_key` the synthesis
submitter supplied. When the submitter is not party A, the record shows a
party-A signature party A never produced. The fix is
[#2450](https://github.com/cirwel/unitares/pull/2450), an open draft. It stands
on its own until D5 lands, since records minted in between should not carry a
signature in a slot whose party did not produce it.

## What this record does not change

- It does not edit the `drr.v1` packet, the threat model, `EVIDENCE_AND_LIMITS`
  or the dormant-capability registry. Open PR #2441 corrects those documents. A
  follow-up can link them to this record after #2441 merges.
- It changes no code, flag, schema or deployment, and it makes no efficacy or
  adoption claim.
