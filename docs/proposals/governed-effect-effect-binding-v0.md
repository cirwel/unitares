# Governed-Effect — Effect-Binding (design v0.2, review-folded)

Successor to the §7 strong-tier re-certification (#1074, live 2026-06-26; design
in `governed-effect-s7-strong-tier-recert.md`). Tracks the one **accepted v0
residual** named there and in #1075: §7 proves the proposer's *identity
strength*, not *per-effect authorization*.

This is a **design pass, not an implementation plan**, and **demand-gated** — the
v0 bounding factors (bearer + §6 + ~1h `exp`) still hold; build only when §8
fires. v0.2 folds an adversarial review pass (code claims re-verified against
source; two critical design holes — enrollment-spoof and a self-contradictory
signed tuple — plus four major gaps, all addressed below).

> **Naming caution:** "effect-binding" names the *Phase 2* property (effect bound
> to *identity*). **Phase 1 binds the effect to its own *content***, not to
> identity — call it *content-binding* / *effect-scoping*. Do not let a future
> status line ("effect-binding live") over-read Phase 1.

---

## 1. The gap (grounded, re-verified 2026-06-28)

§7 forwards the proposer's `continuity_token`; gov-mcp `/v1/effect-veto`
(`src/http_api.py:696`, `recertify_strong_tier` at `:760`) re-certifies it to
`strong` iff the token's HMAC + `exp` verify **and**
`extract_token_agent_uuid(token) == proposer_agent_uuid`. The token attests *"I
am strong-identity A, valid until `exp`."* It says **nothing about this effect**.

Consequences, against the live code:

- **One token authorizes unlimited *distinct* effects** for the token's ~1h
  life. To author a new effect an attacker supplies a new (proposer-chosen)
  `idempotency_key` + a new payload → new server-assigned `effect_id`
  (`gen_effect_id/0` = `:crypto.strong_rand_bytes`, `governed_effect.ex:775`) →
  executes as A.
- **Idempotency does not bound this.** The digest
  `sha256(effect_type ‖ surface ‖ custody_mode ‖ payload_hash)`
  (`idempotency_digest/1`, `governed_effect.ex:107`) only collapses *identical*
  re-submissions (same key + same digest → idempotent replay; same key +
  *different* digest → `:idempotency_conflict`, refused). A *materially
  different* effect under a *new* key is neither deduped nor
  authorization-checked against the token. `idempotency_key` is
  proposer-supplied and required (`:121`, `:143`).
- **Replay window = token TTL.** A captured `continuity_token` (a bearer
  credential) replayed by a holder of `LEASE_PLANE_BEARER_TOKEN` runs effects as
  A until `exp`.

Bounding factors today (why this is an *accepted residual*, not a hole): the
lease-plane bearer gate, the §6 verdict gate, and the ~1h `exp`. As of
2026-06-28 the execute surface broadened — `UNITARES_GOVERNED_EFFECT_EXECUTE_FILE_WRITE`
and `..._FILE_WRITE_COMMIT` flipped on alongside `..._AGENT_SPAWN`
(`application.ex:63-81`). **§9 argues this broadening is not a clean transfer of
the spawn-era acceptability calculus** and may have partially fired §8's first
trigger.

---

## 2. The crux: a bearer credential cannot carry per-effect authorization

The issue's sketch — *"the proposer signs `(effect_id, payload_sha256)` with a
key bound to its identity"* — runs into one hard fact:

> **Agents hold no signing key today.** The only identity-bound secret is the
> single fleet-wide HMAC secret (`_get_continuity_secret`,
> `identity/session.py:164`) that gov-mcp uses to mint *and* verify
> continuity_tokens (`hmac.new(secret, …, sha256)` at `:202`/`:225`). The token
> is `v1.<payload>.<sig>` — the agent possesses bytes, not a private key.

A bearer credential (something whose mere possession is the proof) is, by
construction, **replayable** by anyone who captures it. You cannot derive a
non-replayable per-effect proof from a bearer credential, because producing a
proof that covers *this* effect and no other requires a secret only the
legitimate proposer holds — a **signing key**, not a token.

`src/identity/agent_identity_credential.py` (the AIC prototype) says the same:
`continuity_token` is the *"S19 copyable-bearer vector"*; the AIC fixes
verifiability in the **server→world** direction (Ed25519, server-signed,
non-exportable key, JWKS verify, `resume_capable=false`, `authorizes=[]`) but
**explicitly does not** close the **agent→server** authoring gap:

> *"Not a closure of the S19 agent→server resume-proof gap. That needs the
> enrollment / process pre-registration the plan discusses. This module is the
> verifiable-attestation building block B-strict's 'enrollment certificate'
> idea would stand on."*

So the principled fix is **pre-figured** by the architecture: per-agent **key
enrollment** (agent→server). This design extends that sanctioned building block —
**for Phase 2 only** (see the pedigree caveat in §4-B).

---

## 3. Threat model — three vectors, precisely scoped

| # | Vector | Bound today | What truly closes it |
|---|--------|-------------|----------------------|
| T1 | **Retarget** — a captured proof for effect X authorizes a *different* effect Y | nothing effect-specific | proof bound to `payload_sha256` |
| T2 | **Replay** — a captured execute-*envelope* re-runs the *same* effect | idempotency (same key+digest); a *new* key re-runs | single-use nonce **— but see below** |
| T3 | **Author** — a holder of (bearer **+** captured token) constructs a *brand-new* effect as A | bearer + §6 + `exp` | proposer-held signing key (token alone insufficient) |

**T2 is narrower than it looks.** Decompose:
- *Same idempotency_key replay* → already collapsed by idempotency; the nonce is
  never consulted. Closed today.
- *Same payload, fresh key, **grant-only** attacker (has the grant, not the
  token)* → the nonce is the **only** thing that blocks this. This single
  sub-case is the nonce's entire marginal value.
- *Same payload, fresh key, **bearer+token** attacker* → mints a fresh grant
  with a fresh nonce → runs again. **T3 capability subsumes T2.** The nonce does
  nothing here.

So Phase 1's net-new security over *today* is **T1 closure + replay-window
shrink**, not a freestanding "T2 closed." The table's T2 row must carry: *"closed
only against a grant-only attacker; a token-holder achieves T2 via T3."*

**Closure summary:** server-minted binding (Option B) closes **T1** outright and
the grant-only slice of **T2**; it does **not** close **T3**. Only a
proposer-held key (Option A) or delegation to a key-holding proposer (Option C)
closes **T3**.

---

## 4. Design space

`effect_id` is **server-assigned after** propose (`gen_effect_id/0` at
`governed_effect.ex:409`/`:588`, *before* `GovernanceVetoClient.check/1` at
`:418`/`:594`). So Phase 1 *could* see `effect_id`, but the **proposer** cannot
sign it (it does not exist when the proposer acts). The binding that matters for
T1 is **content** (`payload_sha256`), not the server's handle — see the single
authoritative tuple in §5.

### Option A — per-agent key enrollment (closes T1+T2+T3) — **but enrollment must be atomic with onboard**

A *custody-capable* proposer generates a keypair, keeps the private key in real
custody, and enrolls its **public** key. To propose, it Ed25519-signs the §5
tuple; gov-mcp verifies vs the enrolled public key at `/v1/effect-veto`.

> **CRITICAL: naïve post-onboard enrollment is spoofable and would *widen* T3.**
> If the enrollment endpoint is authenticated by the `continuity_token` — the
> only credential an agent holds post-onboard — then a bearer+token attacker can
> enroll **their own** public key against the victim's UUID (same `aid` check §7
> uses). They have now converted a ~1h token into a **persistent** signing
> capability. T3 is not closed; it is widened.
>
> **The only safe enrollment binds key submission into `onboard` itself**: the
> agent generates the keypair and submits the public key *as part of the onboard
> call*, before any token is minted. At that instant no token exists, so no
> token-holder can front-run the enrollment. Any post-onboard, token-authed
> enrollment inherits the token's entire bearer exposure. This is the S19
> "enrollment certificate / process pre-registration" the AIC module defers to —
> not a token-authed side endpoint.

- **Closes T3 only under two conditions:** (a) atomic-onboard enrollment (above),
  **and** (b) real key custody. Without (b) the agent stores the private key
  beside the token → *capture-one-capture-both* → no T3 gain.
- **Custody is weaker than "dedicated host" implies.** On the live topology
  (macOS, single user) the keychain is **per-user, not per-process** — any
  process in the same user session can request it. So Phase-2 T3 closure is
  honestly scoped to a **remote-capture adversary who cannot read the proposer's
  process memory or per-user keystore** — *not* a local co-resident (the #802
  "irreducible" residual applies to the key exactly as to the token).
- **Verdict:** principled, but pay the cost only where custody is real. Do **not**
  roll per-agent PKI fleet-wide — for the no-custody majority it would make the
  fleet *appear* key-protected while closing nothing (the AIC "not another
  copyable secret in a plist" rule).

### Option B — server-issued effect-scoped grant (closes T1 + grant-only T2; **symmetric, not AIC**)

No agent key. The proposer presents `continuity_token` + the §5 content fields to
gov-mcp at propose-time; gov-mcp verifies the token (existing §7 path) and mints a
**short-TTL (seconds), single-use grant** = `HMAC(secret, tuple)` using the
secret it already holds. The lease-plane carries the grant; `/v1/effect-veto`
verifies it covers the *exact* content + the nonce is unused.

> **Pedigree caveat: Phase 1 is NOT "extends AIC."** The grant is a *symmetric,
> issuer-verifiable-only, copyable* HMAC — the exact property AIC was written to
> escape. Phase 1 is, crypto-ontologically, a **smaller continuity_token**, not a
> small AIC. It is justified **not** by principled pedigree but by **thin
> authority**: a captured grant authorizes *one* effect, *once*, for *seconds*,
> *content-bound* → near-zero standalone bearer value. The fat credential remains
> the token upstream (it mints grants); Phase 1 does not touch it. State this
> plainly — under-claiming the grant's safety is itself a calibration miss.
>
> **Envelope discipline:** give the grant a domain-separated, self-describing
> envelope — `gnt.v1.<payload>.<sig>`, disjoint from `v1.` (token) and `aic.v2.`
> (attestation) so the three can never be cross-verified — carrying explicit
> `authorizes=[payload_sha256]`, `single_use=true`, `exp`. Mirror AIC's
> anti-confusion discipline.

- **Cost:** a propose-time proposer→gov-mcp round-trip to mint the grant (the
  effect path today is proposer→lease-plane→gov-mcp; Phase 1 adds a prior
  proposer→gov-mcp hop), plus a `consumed_nonces` store.
- **Verdict:** a real, cheap win worth doing **even alone** (T1 + window-shrink),
  and a clean staging step toward A/C (same verify-at-veto call site, same tuple).

### Option C — custody-by-delegation / orchestrator-vouched (closes T3 for *mediated* effects)

The original v0.1 framed custody per-*agent* and concluded "T3 stays open forever
for the no-custody majority." That is a **false dilemma**: for the high-impact
execute effects (orchestrated `agent_spawn`, resident-driven `file_write`) the
proposer that actually reaches `/v1/effect-veto` **is** the orchestrator/resident
— which *is* custody-capable — not the ephemeral child.

So the orchestrator (one enrolled key, atomic-onboard per Option A) **vouches for
effects proposed on behalf of the agents it spawned**, signing the §5 tuple with
*its* key. This closes T3 for the **mediated outcomes** of the no-custody
population without handing each ephemeral tab a fake key. The mechanism already
exists as a reviewed RFC: **`docs/proposals/orchestrator-vouched-identity-v0.md`**
(2026-06-17, inert PoC #827, flag-off). Phase 2 should **ride that primitive**,
not re-invent per-agent enrollment.

Residual after C: T3 stays open only for **genuinely unmediated ephemeral
proposers** — a much smaller, honestly-stated surface than "the whole majority."

---

## 5. Single authoritative bound tuple

v0.1 contradicted itself (an early sketch had `effect_id`+`aid`; a later one had
`surface`+`custody` without them). **One definition, both phases:**

```
bound_tuple = (aid, payload_sha256, surface, custody_mode, idempotency_key, nonce, exp)
```

- **`aid`** — binds the grant/sig to the proposer identity (without it, a
  captured grant is presentable by a different `proposer_agent_uuid`).
- **`payload_sha256, surface, custody_mode`** — the content identity (the
  idempotency-digest inputs); this is the T1 anchor.
- **`idempotency_key`** — binds the grant to the specific key, so a captured
  grant cannot be re-presented under an attacker-chosen key to corrupt the
  idempotency audit trail.
- **`nonce`** — single-use (T2, grant-only slice).
- **`exp`** — short freshness (seconds for Phase 1; the §7 ~1h cliff is unrelated).

**`effect_id` is deliberately NOT in the tuple** — the proposer cannot know it at
sign time (Phase 2) and it is not the T1 anchor (content is). gov-mcp records the
verified binding **1:1 with the server-assigned `effect_id`** at veto time, so
"the proof covers the exact content I am clearing" without the proposer needing
the handle. Phase 1 = `HMAC(secret, bound_tuple)`; Phase 2 = `Ed25519(privkey,
bound_tuple)`; Option C = orchestrator's `Ed25519` over the same tuple. Identical
call site, identical fields, different signer — *that* is what "identical" means
(v0.1 wrongly implied identical *trust*).

### Verify-at-veto (extends the §7 endpoint)

`/v1/effect-veto` already computes `tier_ok` (#1074). Add `binding_ok`:

- verify signer (HMAC for Ph1 / enrolled Ed25519 for Ph2/C) **and** tuple matches
  this envelope's content + `aid` + `idempotency_key` **and** nonce unconsumed.
- **Nonce consume must be atomic:** `INSERT INTO consumed_nonces (nonce,
  grant_exp) … ON CONFLICT (nonce) DO NOTHING` with a **rowcount check in the
  same transaction** as the verify — never SELECT-then-INSERT (TOCTOU under
  concurrent submits of one captured grant).
- **Nonce retention ≥ grant exp:** store `(nonce, grant_exp)`, purge on
  `grant_exp < now()`. A fixed retention shorter than `exp` reopens a replay
  window between purge and expiry.
- `vetoed = block_verdict OR block_action OR (not tier_ok) OR (not binding_ok)`.
  Absent/invalid/replayed binding → **fail closed**, like §7. Never a silent
  no-op. Reason string names the gate.

The grant/signature is a **credential**: carried in the `proposer` object
(`@credential_key_substrings` at `governed_effect.ex:70` already bars it from
`payload`), forwarded for verify, **never** persisted to any audit payload.

---

## 6. What this does NOT do (honest residuals)

- **Phase 1 does not close T3.** Bearer+token holder still authors freely.
- **Phase 2/C T3 closure is remote-capture-only** — a local co-resident reading
  process memory or the per-user keystore defeats it, exactly as it defeats the
  token (#802 irreducible).
- **Naïve enrollment widens T3** — only atomic-onboard enrollment is safe; this
  is a hard precondition, not a detail.
- **The bearer remains load-bearing** for unmediated no-custody proposers.
  Effect-binding *reduces* reliance on it; it does not retire it.
- **Forensic-integrity, not just exec-blocking:** binding `idempotency_key`
  (§5) is what prevents audit-trail corruption via cross-key grant reuse.

---

## 7. Composition with existing gates

`/v1/effect-veto` runs orthogonal gates, any of which vetoes: **§6 behavioral
verdict** (#1073) · **§7 identity tier** (#1074) · **§8 effect-binding** (this
doc). §6 = "is this agent misbehaving," §7 = "is the claimed identity really
strong," §8 = "is *this exact effect* authorized." The §7
single-proposer-value TOCTOU fix (derive `env.proposer_agent_uuid` once; reuse
for §6 judgment, §7 `aid` binding, spawn lineage) carries in unchanged and stays
sound.

---

## 8. Deploy + bootstrap

- **Order:** gov-mcp first (understands `binding_ok`, fails closed when absent),
  then lease-plane (starts minting/forwarding). Safe-by-construction.
- **Deploy-window DoS is real and must be in the runbook:** the instant gov-mcp
  requires `binding_ok`, every execute effect *without* a binding is blocked
  until the lease-plane forwards one. Fail-closed is correct, but this is a
  guaranteed execute-surface downtime window, not a silent implication.
- **Enrollment bootstrap:** Phase 2/C needs a per-proposer state machine
  distinguishing *not-yet-enrolled* (allow on Phase-1 grant) from *enrolled but
  bad signature* (block). Without it, enabling Phase-2 enforcement for the
  orchestrator while residents are mid-enrollment strands them. Requires a
  per-proposer `enrollment_state` flag, not a global switch.

---

## 9. Demand calculus — re-run per effect-class, not per surface

v0.1 inherited the spawn-era "bearer + §6 + ~1h exp is acceptable" claim for the
new terminal surfaces. **That does not transfer cleanly:**

- A **spawn** is *downstream-governed* — the spawned agent is itself governed, so
  a captured-token spawn yields a *governed* entity and **§6 has something to act
  on**.
- A **file_write / commit** is a **terminal effect with no downstream
  governance** — it completes before any behavioral signal accrues. **§6 is
  materially weaker** here; the captured-token replay window (~1h) is the dominant
  control, not §6.

So for terminal effects the residual is governed by `bearer + exp` alone, a
weaker bound than for spawns. **§8's first trigger ("tokenless write volume on
the execute path rising materially") arguably *partially fired* the moment
`file_write` went standing-live** — the *target class* changed qualitatively even
if volume hasn't. Recommendation: **split the residual** — keep `agent_spawn` in
the "accepted residual / demand-gated" bucket, but treat **Phase-1 content-binding
for terminal effects (file_write/commit) as the nearest-term increment**, since
its §6 backstop is the weakest.

Build Phase 2/C when a custody-capable, high-impact, externally-reachable proposer
exists where T3 is a credible concern.

---

## 10. Test gates (when built) — "no silent no-op", per #1073/#1074

- **Python (`/v1/effect-veto`):** valid binding for this content → `vetoed:false`;
  **no** binding → `vetoed:true`; binding for a *different* payload (T1) →
  `vetoed:true`; **replayed** nonce (T2 grant-only) → `vetoed:true`; binding with
  a *mismatched `aid`* or *mismatched idempotency_key* → `vetoed:true`; Phase-2
  signature from a non-enrolled key → `vetoed:true`. Provably blocks and allows.
- **Elixir (lease_plane):** execute effect with valid binding commits (202); same
  envelope with binding stripped or payload mutated post-sign → `governance_blocked`
  (403), **nothing executes**. Concurrent double-submit of one grant → exactly one
  commits (nonce atomicity). Assert the grant/sig appears in no `audit.events`
  payload.

---

## 11. References

- §7 recert: `governed-effect-s7-strong-tier-recert.md`, #1074 (`c5feb33c`)
- §6 veto: #1073 · first execute slice: #1067 · Phase-4 design: #1066
- Custody-by-delegation primitive: `docs/proposals/orchestrator-vouched-identity-v0.md` (#827)
- Bearer / enrollment framing: `src/identity/agent_identity_credential.py` (AIC),
  `docs/ontology/identity.md` §S19 / "three stances", `docs/ontology/plan.md` 2026-04-25
- effect_id / idempotency / payload_sha256:
  `elixir/lease_plane/lib/unitares_lease_plane/governed_effect.ex`
  (`gen_effect_id/0` :775, `idempotency_digest/1` :107, `@credential_key_substrings` :70),
  `effect_repo.ex`; execute flags `application.ex:63-81`
- continuity_token primitives: `src/mcp_handlers/identity/session.py`
  (`_get_continuity_secret` :164, `resolve_continuity_token`, `extract_token_agent_uuid`)
- veto endpoint: `src/http_api.py:696` (`http_effect_veto`), `recertify_strong_tier` :760
- Issue: #1075
