# UNITARES Profile of the Governed Effect IR (v0)

**Status:** spec (convergence step 2). Companion to
[`governed-effect-convergence-v0.md`](./governed-effect-convergence-v0.md) and the
Governed-Effect Plane contract ([`governed-effect-plane-v0.md`](./governed-effect-plane-v0.md)).

This defines the **UNITARES profile** of fermata's canonical Governed Effect IR:
how this plane's effect envelope (`POST /v1/effects`, plane §3) maps onto fermata's
IR, and the profile policy that sits *on top of* the portable IR. The pinned IR
schema lives at `tests/fixtures/vendored/`; the parity test
(`tests/test_governed_effect_ir_parity.py`) asserts this mapping produces valid IR.

The portable core stays portable: nothing UNITARES-specific (identity tiers, the
UNITARES effect types, lease custody) goes in the core IR — it all rides
`profile: "unitares"` + the namespaced `profile_ext`.

## Field mapping — plane envelope → fermata IR `Intent`

| plane envelope field | fermata IR `Intent` field | notes |
|---|---|---|
| `idempotency_key` | `idempotency_key` | aligned 1:1 (both require it, both reject same-key-different-intent) |
| `custody_mode` | `custody_mode` | aligned by convergence step 1 (fermata PR #55) — `record_only` \| `execute` |
| `effect_type` | `adapter` + `operation` (core) **or** `adapter: "tool"` + `profile_ext.unitares_effect_type` (UNITARES types) | see effect-type table |
| `surface` | `target` | e.g. `file:///abs` → `target` |
| `payload` | `input` | mode-specific (plane §5); IR `input` is `additionalProperties: true` |
| `proposer` `{agent_uuid, client_session_id}` | `profile_ext.proposer` | UNITARES identity proof — not a core IR concept |
| `provenance` | `profile_ext.provenance` | harness/session/verification source |
| `required_leases` | `profile_ext.required_leases` | lease-plane custody — UNITARES-specific, profile only |
| (derived) | `required_capability` | e.g. `"file.write"` from effect_type |
| (constant) | `profile: "unitares"` | declares the profile |

## Effect-type mapping

| plane `effect_type` | core IR? | `adapter` / `operation` | `profile_ext.unitares_effect_type` |
|---|---|---|---|
| `file_write` | yes | `file` / `write` | — |
| `repo_commit` | yes | `file` / `write` (tree) | `repo_commit` |
| `agent_spawn` | **no** (UNITARES) | `tool` / `spawn` | `agent_spawn` |
| `resident_cycle` | **no** (UNITARES) | `tool` / `cycle` | `resident_cycle` |
| `service_restart` | **no** (UNITARES) | `tool` / `restart` | `service_restart` |

`file_write` is the portable overlap (it exists in fermata's adapters too).
`agent_spawn`/`resident_cycle`/`service_restart` are UNITARES capabilities that
the portable core does **not** know about — they map onto the generic `tool`
adapter and carry their real type in `profile_ext`, exactly so the core IR never
grows UNITARES-specific vocabulary.

## Profile policy (beyond IR validation)

The IR validates *shape*; the profile adds *policy* the IR is deliberately
agnostic about (plane §2):

- **`execute` requires re-verified tier `strong`.** `profile_ext.required_tier`
  must be `strong` for `custody_mode: "execute"`.
- **`record_only` requires tier `≥ medium`**, stamped re-verified (plane §7).
- Identity tiers are a UNITARES concept; fermata's core uses capability/scope.
  The profile is where the tier gate lives, never the core IR.

These policy rules are profile-enforced (the parity test pins the `strong`-for-execute
rule); they are NOT part of fermata's portable contract.

## What this is not

- Not a runtime mapper implementation. The mapping is specified here and encoded
  in the parity test; a code mapper (and the runtime `record_only` behavior in
  fermata) are later steps.
- Not a change to the plane's internals — `EffectCustodian`, the lease plane, and
  `effects.payloads` storage are unchanged. This is the contract-alignment layer.
