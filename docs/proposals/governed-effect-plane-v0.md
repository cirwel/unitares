# Governed-Effect Plane — Protocol Contract v0.1

> **For Hermes:** this is the Phase 2 deliverable of [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md). It is **design-only** and **reviewable**, not an implementation order. No `elixir/` code lands until (a) this contract passes council review — **done, v0.1 folds it in**, (b) the operator names the first effect class, and (c) the 2026-06-24 Wave-3 gate read. Read the dossier's Council Amendment first.

**Created:** 2026-06-18 · **Status:** Draft v0.1 — council-revised (3 fact corrections + 8 implementability holes + 4 open decisions resolved)

## Council revision log (v0 → v0.1)

- **Fact fixes (live-verifier):** identity tiers are **`strong / medium / weak`** (not the 4-value list v0 invented; `caller_proven` is a boolean, `caller_asserted` is a `proof_origin` value); the `audit_event` table **does not exist** — real sinks are `audit.events` / `audit.outcome_events`; idempotency logic lives at `repo.ex:77-125` (`acquire_step/3`), not the doc/constant lines v0 cited; `surface_registry.ex` is an in-memory spike, the durable path is `Repo.acquire/1`.
- **Decisions resolved:** §8 → reuse `audit.events` + `effect_lane` tag for v0, defer a dedicated table to the execute-promotion migration. §4 → custody bound reframed to `min(lease.expires_at)`, checked at propose **and** commit. §2/§4 → `record_only` **observes, does not acquire** leases. §5 → layered global + per-class payload ceilings.
- **Holes closed:** added a state-transition table (§3); named the executor model (`EffectCustodian`, §5a); defined the idempotency digest (§4); specified payload storage + scrub contract (§5); added the `governance_blocked` veto interface (§6); struck the fragile "infer mode from error" tell (§6).
- **Reopened by council, now adopted:** `repo://unitares/doc_update` is the first **`record_only`** shadow but **not** the first `execute` surface (a no-contention surface never exercises the veto/single-winner membrane); the first `execute` surface must be cheap-but-contended (§10). `record_only` shadows feeding promotion must carry a re-verified tier so weak attribution can't count as readiness evidence (§7).

---

## 1. Scope

The wire + lifecycle contract for a **governed effect**: an agent's proposal to mutate a surface, mediated under custody, in one of two modes. It does **not** stand up a runtime, does **not** pick the vehicle (effect class decides: agent-spawn → `agent_orchestrator`; content → lease-plane envelope), and does **not** name the first `execute` surface (operator call). The schema is identical across vehicles; only the executor differs.

## 2. Custody modes

| mode | what BEAM does | what BEAM may CLAIM | identity floor (re-verified server-side) |
|---|---|---|---|
| `record_only` | validates identity/provenance, **observes** (does not acquire) the `required_leases` and records would-acquire state, assigns a durable `effect_id`, emits typed telemetry | **nothing about the side effect** — the proposer/external actor still executes. Shadow / dry-run / replay. | tier ≥ `medium`, **and** the row is stamped with the re-verified tier so promotion can exclude weak shadows (§7) |
| `execute` | holds `required_leases`, owns the bounded payload, calls the governance veto check, **performs/delegates the commit** under OTP supervision, emits the terminal fact | "agents propose, BEAM commits" — the genuine new safety property | tier == **`strong`** (the server's actual gate, `phases.py:345`) |

**Rhetoric discipline:** "membrane" / "custody-of-act" language is valid **only** in `execute`. `record_only` is shadow custody / proposal logging — it never commits.

A surface may begin `record_only` and **promote** to `execute` behind a fail-closed flag, once payload/idempotency/rollback are specified and the gate read passes. Promotion reads the shadow record — so shadow attribution quality is load-bearing (§7).

## 3. Effect envelope + state machine

```jsonc
// POST /v1/effects
{
  "idempotency_key": "uuid-or-content-hash",          // REQUIRED, proposer-supplied — see §4
  "effect_type":     "file_write|repo_commit|agent_spawn|resident_cycle|service_restart",
  "surface":         "repo://unitares/doc_update | file:///abs | agent:/<id> | resident:/<name>",
  "custody_mode":    "record_only|execute",            // REQUIRED, explicit — no default
  "proposer":   { "agent_uuid": "...", "client_session_id": "..." },   // proof, re-verified; NOT a self-asserted tier
  "provenance": { "harness": "...", "session_id": "...", "verification_source": "..." },
  "payload":         { /* mode-specific — §5; NOT an opaque ref */ },
  "required_leases": [ { "surface": "...", "ttl_s": 300 } ]
}
```

```jsonc
// 202 accepted — custody_mode echoed in EVERY response (never inferred)
{ "effect_id": "...", "custody_mode": "record_only|execute",
  "status": "proposed|held|committed|rejected|revoked|custody_expired",
  "custody_expires_at": "RFC3339" }
```

**State transitions** (`(from, event) → to`):

| from | event | to | modes |
|---|---|---|---|
| — | `POST /v1/effects` accepted | `proposed` | both |
| `proposed` | `record_only` lease-observation recorded | `committed`¹ | record_only |
| `proposed` | `execute` acquires all `required_leases` | `held` | execute |
| `proposed`/`held` | acquire fails (held_by_other) | `rejected` | execute |
| `held` | governance veto check passes + commit applied | `committed` | execute |
| `held` | governance veto check fails | `rejected` | execute |
| `proposed`/`held` | a `required_lease` `expires_at` passes (or force-released) | `custody_expired` | both² |
| `proposed`/`held` | operator force-release of effect custody | `revoked` | both |

¹ `record_only` has no commit; `committed` here means "shadow record finalized," and the row is explicitly tagged `custody_mode=record_only` so it is never read as a real commit. (`held` is **execute-only** — that resolves v0's ambiguous `held`.)
² terminal for `proposed`/`held`; a `committed` effect is never retro-expired.

## 4. Idempotency + custody validity (holes a, b)

**Idempotency digest.** `idempotency_key` is REQUIRED. The server computes a canonical digest = `sha256(effect_type ‖ surface ‖ custody_mode ‖ payload_hash)` — **excluding** `provenance.*` and `proposer.*` (a retry from a new session for the same logical effect must not look "materially different"). Same key + same digest → returns the existing `effect_id`/status (mirrors the lease plane's same-holder idempotent re-acquire, `repo.ex:77-125`). Same key + different digest → `idempotency_conflict`. A *different* key proposing the *same exclusive surface* → `lease_held`.

**Custody validity (reframed from v0's broken `≤ ttl_s`).** `ttl_s` is a *renewal interval* for `local_beam` leases, not a lifespan — so v0's `custody_ttl ≤ min(required_leases.ttl_s)` was incoherent. Correct rule:

- **Invariant (always):** custody is valid only while `now < min(required_leases.expires_at)`. Checked at **both** propose time and **commit time in the same transaction as the mutation** — an `execute` effect whose lease was force-released mid-flight must re-confirm ownership before committing, or you commit under a revoked lease.
- **`record_only`:** observes only (never holds), so there is nothing to release; the shadow row self-expires at its own TTL. No heartbeat, no lease leak.
- **`execute`:** custody is held by the executing GenServer (§5a), which is **also** the lease renewer. Custody rides forward as `expires_at` advances; if the GenServer dies it stops renewing *and* owning simultaneously, so lease + custody reap together (no zombie lease). To keep the deadline a hard one, **`execute` custody may hold only `remote_heartbeat`-routed leases** (`file://`, `agent:/`); `local_beam` auto-renew is disallowed for execute custody so expiry is always a real deadline.

## 5. Payload contract + storage (hole c)

Opaque refs are **rejected** (no integrity, no forensic value). Mode-specific:

| mode | payload | bound |
|---|---|---|
| `record_only` | `sha256` of the intended mutation + a redacted summary string (≤ 512 chars). No raw bytes. | tiny by construction |
| `execute` | the actual bytes/command the custodian applies (file content, argv, commit tree-ish). | ≤ **per-class ceiling ≤ global hard backstop** (layered, §11 resolved) |

Promotion continuity: an `execute` payload must hash-match the `sha256` its `record_only` predecessor recorded — proving the executed bytes are the ones shadowed.

**Storage.** `execute` payloads persist in Postgres in a **new `effects` schema** (table `effects.payloads`: `effect_id PK`, `custody_mode`, `payload_bytes bytea`, `payload_sha256`, `created_at`) — a **new manual migration** (next free slot; migrations are a manual, incident-prone surface per CLAUDE.md, so the slot is assigned at impl time, not pre-claimed here). Never logged raw.

**Scrub contract (Invariant 7).** Two layers: (1) **caller obligation** — proposers must not place continuity tokens / bearer creds in a payload; (2) **server backstop** — before storage the plane strips a named key-blocklist (`client_session_id`, `continuity_token`, `*_token`, `authorization`, `bearer`) from any structured payload and rejects (`schema_invalid`) a payload whose summary matches a credential-shaped pattern. Telemetry/audit rows carry the `sha256` and summary, never the bytes.

## 5a. Executor model (hole: who commits?)

`execute` custody is a **new `EffectCustodian` GenServer**, not a reuse of `AgentRunner` (which acquires only its own single `agent:/<id>` lease at spawn and has no path from a Postgres-stored payload to a Port). `EffectCustodian` follows the *same OTP pattern* as `AgentRunner` (`init/1` acquire → `terminate/2` release) but: acquires the envelope's N `required_leases`, correlates them to `effect_id`, retrieves the payload from `effects.payloads`, and dispatches to a per-`effect_type` executor behaviour:

```elixir
@callback apply_effect(effect_id, payload, leases) :: {:committed, term} | {:rejected, reason}
```

Each `effect_type` (`file_write`, `repo_commit`, …) implements `EffectExecutor`. Whether an executor runs in-process (`System.cmd`) or via a delegated `Port` (the `AgentRunner` pattern) is the executor's own choice; the custodian only requires the behaviour. For `agent_spawn`, the executor *is* `AgentRunner` — the one case where the vehicle is the orchestrator.

## 6. Typed error vocabulary

| error | meaning | caller action |
|---|---|---|
| `schema_invalid` | envelope/payload failed validation (incl. credential-shaped payload) | fix, resubmit |
| `identity_required` | no caller proof on an execute/write-class effect | bind, resubmit |
| `insufficient_assurance` | re-verified tier below the effect's floor (§2/§7) | re-bind higher or downgrade to `record_only` |
| `lease_held` | a *different* holder owns an exclusive `required_lease` | back off / request handoff |
| `idempotency_conflict` | same key, different digest (§4) | regenerate key |
| `governance_blocked` | governance vetoed the commit (`execute` only) | read guidance; the new safety gate fired |
| `revoked` | operator force-released the effect's custody | re-propose |
| `custody_expired` | `proposed`/`held` effect outlived `min(lease.expires_at)` | re-propose |

**Governance veto interface (`governance_blocked` trigger).** Before committing, `EffectCustodian` calls the UNITARES governance check (HTTP `POST` carrying `effect_id`, `proposer.agent_uuid`, `surface`, `payload_sha256`); a `{"ok": false, "vetoed": true}` (or governance `pause`/`reject` verdict) → `governance_blocked`. The proof is the proposer's `client_session_id`, re-verified server-side (Invariant 1: BEAM consumes proof, never mints it). *(Mode is always explicit in the response and the audit row — v0's "infer execute-mode from the presence of `governance_blocked`" tell is **struck** as a fragile side-effect-as-state pattern.)*

## 7. Identity (corrected to the live system)

The running tiers are **`strong` / `medium` / `weak`** (`phases.py:68-71`, `_TIER_SCORES`). `caller_proven` is a boolean attribute (true when `proof_origin == "caller_asserted"`), **not** a tier. `execute` gates on `tier == "strong"` — the server's actual gate (`phases.py:345`), re-verified, never trusting a self-asserted claim.

`record_only` floor is **`medium`** (not `weak`): shadow records are the evidence base for execute-promotion, and the #839 identity-bleed lesson is precisely that server-inferred context must not pass as caller-owned. Every shadow row is stamped with its **re-verified** tier so promotion can filter out low-attribution shadows rather than trusting them as readiness evidence.

## 8. Durable recording (Open Q3, resolved)

**v0 records governed-effect lifecycle to the existing `audit.events` stream with a mandatory `effect_lane` tag — NOT `outcome_event`, NOT a new table yet.** Reasoning: `outcome_event` feeds the EISV/prior-state predictive matrix (`outcome_events.py:138-151,366-388`), and Invariant 5 forbids effect telemetry masquerading as EISV evidence — so it stays out. But `audit.events` is already a forensic sink *outside* the predictive slice; the #846 lesson justifies avoiding `outcome_event`, not splitting `audit.events`. Once `effect_lane` is a required discriminator, `WHERE effect_lane IS NOT NULL` is the same partition a dedicated table would give, at zero migration cost. A dedicated `governed_effect_events` table (with constraint-backed typed commit columns) is promoted **as part of the execute-promotion migration** (Phase 4), when commit-bearing fields earn their own constraints — not now. *(`audit_event` as named in v0 does not exist; the real sink is `audit.events`.)*

## 9. Relationship to existing surfaces (no double-booking)

- `required_leases` are **real lease-plane leases** (`/v1/lease/acquire`); the envelope wraps lease custody, never reimplements it. `surface_registry` / `Repo.acquire/1` remains single-winner authority.
- Custody transfer reuses `/v1/lease/handoff/{offer,accept}` — but note the verified constraint: handoff is release-and-reacquire that **always** mints a `remote_heartbeat` lease for the recipient, so a receiving `EffectCustodian` must heartbeat (renew) or the transferred custody self-heals/reaps.
- Revocation reuses `/v1/lease/force-release` + `LEASE_FORCE_RELEASE_TOKEN` (live + enforced) — no second revocation mechanism.

## 9a. Relationship to fermata

`fermata` (separate repo — the "governed-effect runtime seed") and this Governed-Effect Plane both explore the same primitive — *agents propose effects; only governed effects commit* — but as of **2026-06-22** they are **independent tracks**, not one implementation under two names. This plane is the in-repo realization: it reuses the lease plane (`/v1/lease/acquire`, §9) and a new `EffectCustodian` GenServer (§5a), and does **not** depend on, vendor, or supersede fermata. fermata remains its own seed.

This is a deliberate *"independent for now"* call, not a supersession. Convergence is allowed later — fermata adopting this contract, or this plane extracting toward it — but requires an explicit decision amending this note. Until then: **cross-reference, do not couple**, and neither track should treat the other as its implementation.

## 10. First surfaces (operator decision 2026-06-18: **both classes**, one role each)

The operator chose **both** effect classes — mapped onto the council's record_only/execute split so it remains one shadow + one enforced surface (the one-enforced-surface discipline holds):

- **First `record_only` shadow → content class: `repo://unitares/doc_update`** (vehicle: **lease-plane effect envelope**). The only candidate with no existing lease coverage, lowest blast radius, and (per the stash episode) a surface where uncoordinated contention demonstrably bites. Proves the envelope plumbing; no contention required for a shadow.
- **First `execute` surface → agent-spawn class** (vehicle: **de-inert `agent_orchestrator`**). This is the council's "cheap-but-*contended*" requirement satisfied on the contention axis: the orchestrator already owns `agent:/<id>` presence leases, supervision, and restart, so the `lease_held` / handoff / single-winner veto membrane actually gets exercised — which a no-contention doc-write never could.
  - **Cost the operator is accepting (named, not cheap):** de-inerting the orchestrator stands up a localhost endpoint that **spawns OS processes** (RCE-class if the bearer leaks). Mitigations are real (localhost-only bind, fail-closed bearer → 503 when `AGENT_ORCHESTRATOR_BEARER_TOKEN` absent), but this is a heavier security surface than the doc shadow. This is the trade agent-spawn buys: contention now, at higher blast radius than a content effect.
  - **Gated:** the orchestrator de-inerting itself (plist + bearer + first caller) waits for the 2026-06-24 Wave-3 read and a re-confirm; nothing flips on from this doc.

A `repo://` surface is **not** promoted to execute by default — if it ever is, reconcile `repo://` against the underlying `file://` lease layer + the gov-plugin per-edit release discipline (the file-lease-leak history) first.

## 11. Gates remaining + still-open

Closes Phase-3 gate (a). Gate (b) — operator names the first effect class — **closed 2026-06-18: both classes, per §10** (content/`doc_update` shadow on the lease-plane envelope; agent-spawn execute on the de-inerted orchestrator). Still required before `elixir/` code: only the **2026-06-24 Wave-3 read** (gate c). With both vehicles chosen, Phase 3 can build two tracks in parallel once the gate opens — the lease-plane envelope (shadow) and the orchestrator de-inerting (execute).

Genuinely open (smaller, deferrable to impl):
1. The `effects.payloads` schema columns + migration slot (assigned at impl time).
2. Exact global payload backstop value and the conservative per-class default.
3. The governance-veto endpoint path/shape (stubbed in §6; finalize when `execute` is built).
