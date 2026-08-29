# Attestation issuance scope — v0

**Status: design-only. No code, no widening, no decision taken.** This is the
first design question named by
[`beam-verbs-as-contract-capabilities-v0.md`](beam-verbs-as-contract-capabilities-v0.md)
§9 for its steps 1 and 2, split out because it is a security question about the
signer that stands on its own and is worth deciding before anything depends on
the answer.

**Review: none.** Written in one pass from a reading of the tree. Every claim
below cites the call site it came from; re-derive before relying on it.

**It does not unblock those steps.** They additionally wait on §5's accounting
choice, a server-enforced idempotency key, and §8's claim/ack semantics. This
closes one prerequisite, not the set.

---

## 1. The question

`mint_lease_attestation` refuses any path that is not `/v1/lease/...`
(`validate_path`, `src/lease_attestation.py:176-184`). The server-side minting
pattern the BEAM-verbs RFC depends on therefore cannot produce a proof for
`/v1/msg/send` or `/v1/msg/inbox`. What, precisely, should govern the set of
routes governance is willing to bind a proof to?

## 2. What is already settled — do not re-litigate

Widening the allowlist **does not enlarge any existing token's audience.** Every
attestation carries an exact `pth` claim and BEAM compares it against the exact
request path (`federated_identity_verifier.ex:95`, `claims["pth"] ==
context.path`), alongside exact `mth`, `bsha`, `sub`, and `aud` comparisons. A
proof minted for one route can never be presented at another, whatever the mint
would now permit. The RFC records an earlier draft getting this wrong.

What widening changes is the **signer's issuance scope**: the set of routes
governance is willing to bind *future* proofs to. That is the whole of the
security question, and §3 is why it is sharper than it looks.

## 3. ⛔ The allowlist is doing two jobs, and only one of them is obvious

`validate_path` is not only scoping what governance will sign. It is also the
sole containment on a deliberate assurance exemption.

There are exactly two mint call sites:

| Call site | Assurance gate | Paths |
| --- | --- | --- |
| `POST /v1/lease-holder/attest` (`http_routes/lease_identity.py:132-140`) | **`recertify_strong_tier`** — a caller-presented, unexpired continuity proof whose `aid` matches the claimed holder; denies otherwise (`lease_identity.py:110-122`) | caller-supplied |
| `_mint_presence_attestation` (`mcp_handlers/identity/agent_presence_lease.py:145-160`) | **none** | `/v1/lease/heartbeat`, `/v1/lease/acquire` (lines 106, 133) |

The second is exempt **on purpose**, and the reason is sound. Its docstring:
"Mint a request-bound proof after onboarding has established the UUID … It
avoids the bootstrap loop that would result from asking an onboarding call to
present the continuity token it has not returned yet." An onboarding call cannot
present the token onboarding has not yet issued.

So governance already contains one internal mint that signs on the strength of
"onboarding established this UUID" rather than on the strong, caller-proven bar
the RFC's §7 invariant demands. Today the only thing stopping that exemption
from reaching any other route is the `/v1/lease/` prefix in `validate_path`.

**A single global widening therefore silently extends a bootstrap exemption to
the message mailbox.** `authorize_strict` on the msg routes would accept the
resulting proof — it checks the signature and the claims, not which internal
code path chose to sign. That is precisely the laundering the RFC's §2 rule
exists to prevent, arriving through the back door rather than the front.

This is not a hypothetical about future code. It is a property of the two call
sites that exist right now.

## 4. Proposal: scope belongs to the mint site, not to the signer

Issuance scope should be a property of *who is asking governance to sign*, not a
single list the whole process shares.

- **`mint_lease_attestation` stops accepting a free-form path.** It takes a
  declared scope, and the path must be a member of it. There is no "any path
  the signer is willing to sign" set any more, because that set is what §3
  shows to be unsafe.
- **Each mint site declares an explicit set of exact `(method, path)` pairs.**
  Exact pairs, never a prefix: a prefix is not enumerable, so no reviewer can
  answer "what may this site sign?" by reading it, and a later route added under
  the prefix joins the scope with no diff and no review. An exact set makes the
  commit that widens a scope *be* the security review.
- **The presence site keeps its exemption and keeps its two paths.**
  `{POST /v1/lease/heartbeat, POST /v1/lease/acquire}`. Unchanged behavior,
  but the exemption is now bounded by something that says so, rather than by a
  prefix that bounds it as a side effect.
- **The attest route keeps `recertify_strong_tier`** and carries the scope it
  effectively serves today.
- **A future `msg` capability mint is its own site**, strong-assurance-gated per
  the RFC's §7 invariant, scoped to `{POST /v1/msg/send, POST /v1/msg/inbox}`
  and nothing else. It cannot inherit the presence exemption because it is not
  the presence site.

The property this buys, stated so it can be tested: **no mint site can sign for
a path outside its declared scope, and the site that skips the assurance gate
has a scope that contains only the two bootstrap paths.** A test should pin both
halves — and in particular that the presence site cannot mint for `/v1/msg/*`,
which is the regression §3 describes.

`validate_method` already restricts to POST (`lease_attestation.py:167-173`);
both msg routes are POST, so nothing here needs it relaxed. That restriction
should stay.

## 5. Defence in depth: a distinct audience per surface family

`aud` is currently one configured value for the whole lease-plane deployment
(`configured_audience`, `lease_attestation.py:111-121`), compared exactly at
verification. Giving the message surfaces their own audience would make a msg
proof fail the audience check at a lease surface and vice versa — a second,
independent barrier that does not depend on `pth` being right.

**Named, not proposed, because its cost is real and falls outside this
document.** BEAM resolves one `configured_audience` per node
(`federated_identity_verifier.ex:24,75`), so a second audience means the node
must accept a set rather than a value, and the rollout has an ordering
constraint: the verifier must accept the new audience *before* the signer emits
it, or every proof minted in the gap is refused. `pth` already makes cross-route
presentation impossible, so this is hardening rather than a fix. It belongs
with whoever changes the verifier, and should not be smuggled into a
Python-side scope change.

## 6. What this does not do

- **No code.** Widening issuance scope with no consumer would ship an unwired
  surface, and an unwired surface is the second of the four states a later zero
  cannot distinguish. The scope change lands with the mint site that needs it.
- **Does not unblock** BEAM-verbs §9 steps 1 or 2. It closes one of their
  prerequisites.
- **Does not touch verification.** No claim, comparison, or refusal on the BEAM
  side changes.
- **Does not narrow anything in use.** The presence site keeps exactly the two
  paths it mints for today.

## 7. How to know this was wrong

- If a reviewer cannot answer "what may this mint site sign?" by reading the
  site's declaration, the scope is not enumerable and §4 failed.
- If any mint site's scope has to grow to accommodate a caller that is not that
  site's own purpose, the scope boundary was drawn around the wrong thing.
- If the presence exemption ever needs a path outside its two bootstrap routes,
  the bootstrap argument has stopped applying and the exemption should be
  re-derived from scratch rather than extended.
- If a future msg mint site is written to reuse the presence helper rather than
  declare its own gate and scope, §3's laundering path has been recreated and
  this document did not prevent it.
