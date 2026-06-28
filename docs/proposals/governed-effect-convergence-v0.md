# Governed-Effect Convergence — fermata as contract, plane as implementation (v0)

**Status:** DECISION RECORDED 2026-06-28 (operator: "let's unite"). Supersedes the
"independent for now" call in `governed-effect-plane-v0.md` §9a.

**The decision:** the two implementations of the governed-effect primitive — the
`fermata` seed (separate repo) and this in-repo Governed-Effect Plane — **converge**.
**fermata owns the canonical contract; the plane becomes a UNITARES *profile* of it
plus the BEAM executor.** They stop being two parallel primitives and become
**one contract, two engines** (portable Python seed + in-fleet BEAM realization).

Rationale: the primitive is identical (*agents propose; only governed effects
commit*); maintaining it twice in two languages drifts (this doc proves the drift
already started); the BEAM migration widens the gap weekly, making "converge later"
steadily more expensive. fermata already ships the right shape for a contract: a
versioned schema (`governed-effect-ir-v0.schema.json`), a contract doc, and golden
fixtures.

## Gap analysis — where the two contracts have already diverged

Both share the core (propose → govern → commit, idempotency keys, SHA-256,
scope/capability), but they evolved apart on four axes:

| Axis | fermata (IR v0) | Plane (v0.3) | Reconciliation |
|---|---|---|---|
| **State model** | 8-state linear machine: `proposal → intent → admissible → verified → approved → committed` (+ `rejected`/`paused`) | 2 custody **modes**: `record_only` (shadow) / `execute` (commits), with a promotion gate | The plane's modes are *subsets* of fermata's states. Add a `custody_mode` facet to the IR; map `record_only` ≈ reaches `verified`, never `committed`; `execute` ≈ `approved → committed`. |
| **Effect-type vocab** | dot form: `file.write`, `memory.write`, `network.fetch` | underscore form + UNITARES-specific: `file_write`, `repo_commit`, `agent_spawn`, `resident_cycle`, `service_restart` | Pick **one** spelling in the IR (recommend dot). Core types live in the IR; `agent_spawn`/`resident_cycle`/`service_restart` register as a **UNITARES profile extension**, not core. |
| **Verification** | post-commit **read-back + SHA-256** ("verified" state) | pre-execute **promotion hash-match** (execute bytes must match the `record_only` sha256) | Complementary, not competing. The IR names **both**: continuity hash-match (pre) *and* read-back verify (post). |
| **Identity coupling** | capability/scope + provenance (portable, no tier system) | proposer tier **re-verified**, `execute` requires `strong` (binds `phases.py:359`) | Keep the **core IR identity-agnostic** (capability/scope). The strong-tier gate is a **UNITARES profile** policy layered on top — so fermata stays portable for non-UNITARES orchestrators. |

Aligned already (no work): idempotency keys (both require proposer-supplied,
both reject same-key-different-intent), SHA-256 as the integrity primitive,
scope/capability admission, the propose→admit→commit spine.

## Unified design

```
fermata: Governed Effect IR (canonical contract)
   = states + envelope + idempotency + scope + sha256 verify
   + a PROFILE/extension mechanism (custody_mode facet, extra effect_types, extra policy gates)
        ▲ targets the contract            ▲ targets the contract
   portable Python adapters          UNITARES profile
   (file/memory/network)             = identity-tier gate + UNITARES effect_types
                                        + BEAM EffectCustodian executor (§5a)
```

- **The portable core stays portable.** No UNITARES identity tiers, no
  `agent_spawn`, in the core IR — those are a profile. A third-party orchestrator
  can target fermata without importing UNITARES.
- **UNITARES becomes a profile, not a fork.** The plane's `record_only`/`execute`,
  the strong-tier execute gate, the UNITARES effect types, and the
  `EffectCustodian` GenServer are all expressed as "the UNITARES profile of IR v0
  + a BEAM executor for it."

## Migration steps (sequenced; each is small and independently shippable)

1. **Contract first (fermata):** add the `custody_mode` facet and the
   profile/extension mechanism to `governed-effect-ir-v0.schema.json`; promote to
   `v0.1`. Add golden fixtures for a `record_only` and an `execute` envelope.
2. **Profile spec (this repo):** write the "UNITARES profile of IR v0.1" — the
   effect-type registrations + the strong-tier execute gate as profile policy.
   No code change yet; spec only.
3. **Schema parity test:** a test in this repo that loads fermata's IR schema (as
   a vendored copy / pinned version — fermata ships packaged schema copies) and
   asserts the plane's envelope validates against it. This is the anti-drift
   guard — the thing whose absence let them diverge.
4. **Reconcile vocab:** pick the canonical effect-type spelling; alias the
   underscore forms during transition.
5. **Executor stays put:** the BEAM `EffectCustodian` (§5a) does not move — it is
   the UNITARES profile's executor. Only its *contract surface* (envelope shape,
   states) aligns to the IR.

## What does NOT change

- The plane's lease-plane reuse (§9), the `EffectCustodian` OTP model (§5a), the
  `effects.payloads` storage (§5), the scrub contract (Invariant 7), the v0.3
  rollback/reversibility work (§5b) — all stay. Convergence is at the **contract**
  layer, not a re-platforming.
- fermata stays its own repo and its own portable seed. "Unite" means *one
  contract*, not *one codebase*.

## First concrete action

Step 1 (the contract change in fermata) is the unblocking move — everything else
targets it. It is also the safest place to start: a schema + fixtures change in
the seed repo, no live-surface risk, no collision with in-flight plane/BEAM work.
