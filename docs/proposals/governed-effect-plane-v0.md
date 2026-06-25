# Governed-Effect Plane — Protocol Contract v0.3

> **For Hermes:** this is the Phase 2 deliverable of [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md). It is **design-only** and **reviewable**, not an implementation order. No `elixir/` execute code lands until (a) this contract passes council review — **v0.3 folds a Phase-4 council pass**, (b) the operator names the first effect class — **closed**, (c) the 2026-06-24 Wave-3 gate read — **done; BEAM-stands**, and (d) **rollback/reversibility specified (§5b)** + **the governance veto endpoint built (§6 — it does not exist yet)**. Read the dossier's Council Amendment first.

**Created:** 2026-06-18 · **Status:** Draft v0.3 — Phase-4 readiness, council-corrected. Adds rollback/reversibility (§5b), execute build sequencing (§12), first-execute-surface revised to `file_write` (§10).

> **What v0.2→v0.3 changes (Phase 4 enablement, 2026-06-25):** §1–§9 of the contract are otherwise as v0.1. The record_only durable-recording path of §8 is now **shipped + live** (#1065 — `audit.events` + `effect_lane`, idempotency). This revision adds the things that gate the *execute* half: a rollback/reversibility contract (§5b, the named-but-unwritten prerequisite of §2), a dry-run-first build sequencing (§12), and an operator revision of the first live-execute surface from `agent_spawn` to `file_write` (§10) on blast-radius grounds.

## Council revision log (v0.2 → v0.3, Phase-4 pass)

A 3-lane adversarial council (architect + reviewer + live-verifier) reviewed the v0.2 draft. Net verdict: **the execute half is no-go until the items below are built; the `record_only` half (#1065) is unaffected and keeps accruing evidence.** Folded in:

- **Crash recovery rewritten (BLOCKER, all 3 lanes).** Blind "roll back any uncommitted effect" was wrong: it would restore a pre-image *holding no lease* (custodian crash reaps the lease → clobbers a competing writer), and the dual-write between the surface and the `committed` mark made it revert real commits. Replaced with **content-hash reconciliation** (§5b) + **re-acquire lease before restore** + `restart: :transient` + a boot-time `EffectRecovery` scanner + a **min-TTL floor** for execute leases. The inherited `restart: :temporary` (`lease_supervisor.ex:43`) would never have fired the recovery path.
- **Idempotency contradiction resolved (BLOCKER).** §6's `commit_failed` "retry replays" contradicted §4 + the durable #1065 row (retry = silent no-op). Now a rolled-back effect's shadow row is **tombstoned** so a same-key retry re-executes (§5b).
- **Veto endpoint is fictional (REFUTED, live-verifier).** §6's `{"ok":false,"vetoed":true}` shape exists nowhere on `:8767`; flagged as a hard build prerequisite, with the real closest surface (`dialectic`) and its actual shape noted.
- **`phases.py:345` corrected to `:359`** (REFUTED) — and noted that no execute-custody tier gate exists yet; the strong-check is `require_strong_identity`-gated (§7).
- **`repo_commit` "unobserved" struck** (undecidable) → decidable-conjunct guard only (§5b). **`quarantined` terminal-dirty state added** to §3. **Promotability split from reversibility** — irreversible-but-veto-gated types promote under an explicit no-rollback acknowledgment, not a permanent `record_only` bar (§5b). **§12 startup enforcement** given a concrete hook. **§10 "dominates every axis" overclaim** corrected to an honest blast-radius tradeoff.
- **Confirmed solid:** `file://` leases *are* `remote_heartbeat`-routed (DB-CHECK-enforced) so §4/§10 holds; `/v1/lease/force-release` + `/v1/lease/handoff/*` exist and behave as §9 claims.
- **Carried as a follow-up (not this doc):** a latent `CaseClauseError` in the shipped `governed_effect.ex` idempotency lookup when a stored row has a null `idempotency_digest` (today unreachable; harden in a code PR).

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
| `execute` | holds `required_leases`, owns the bounded payload, calls the governance veto check, **performs/delegates the commit** under OTP supervision, emits the terminal fact | "agents propose, BEAM commits" — the genuine new safety property | tier == **`strong`** (binds the existing strong-check at `phases.py:359` onto the execute path — to-be-built, see §7) |

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
| `held` | apply failed **and** compensation failed / third-party write detected (§5b) | `quarantined`³ | execute |

¹ `record_only` has no commit; `committed` here means "shadow record finalized," and the row is explicitly tagged `custody_mode=record_only` so it is never read as a real commit. (`held` is **execute-only** — that resolves v0's ambiguous `held`.)
² terminal for `proposed`/`held`; a `committed` effect is never retro-expired.
³ **terminal-but-dirty** (added v0.3, council). The surface may be corrupt and the compensation could not restore it. Lease disposition: **the custody lease is HELD, not released** — the surface stays unacquirable so no other writer can build on corruption — and the operator is paged. The only exit is operator inspection + `/v1/lease/force-release`. Accepted tradeoff: a single dirty surface is a localized DoS-until-operator, which is strictly safer than letting the corruption be overwritten/compounded.

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

## 5b. Rollback / reversibility (the named-but-unwritten promotion gate)

§2 gates execute-promotion on *"payload/idempotency/**rollback** are specified."* Idempotency is shipped (§4, #1065); payload storage is §5; rollback was named and never written. This section writes it — and the writing surfaces the load-bearing fact that **not every effect is reversible**, which is *why* the first live-execute surface changed (§10).

### The commit boundary is the only irreversibility line

The normal execute path is `held → re-verify custody+tier → governance veto (§6, BEFORE commit) → apply → committed`. The veto fires *before* the mutation, so the governance gate is itself the primary "don't do this" mechanism. **Rollback is not "undo a committed effect."** A `committed` effect is terminal (§3 note ²: never retro-expired); reversing it later is a *new forward compensating proposal*, not a rollback. Rollback is scoped narrowly to the **`held → committed` apply window**: a partial/failed apply, or custody lost mid-apply (lease force-released, custodian crash). What rollback must guarantee: **an effect that did not reach `committed` leaves the surface as if it never started.**

### Reversibility classes (per `effect_type`)

An executor declares a reversibility class; the class decides whether the type may ever flip to live `execute`:

| `effect_type` | class | compensation (within the apply window) | promotable to live execute? |
|---|---|---|---|
| `file_write` | **reversible** | capture **pre-image** (prior bytes + `existed?` flag) before the write; rollback restores the pre-image, or deletes the file if it did not exist | **YES — first (§10)** |
| `repo_commit` | **conditionally reversible (decidable guard only)** | record pre-commit `HEAD`; fast-revert (`reset --hard <pre>`) allowed **only** on the conjunction of **decidable** facts — `unpushed AND HEAD still == our post-commit tip AND working tree clean of other writers AND lease still held by us`; otherwise a forward `revert` commit. *"Unobserved" is struck — it is undecidable (you cannot detect another actor's `git status`/CI/editor read), and `reset --hard` on that premise destroys work* | gated — needs the decidable-conjunct guard built; never `reset --hard` on a probabilistic "no one looked" |
| `agent_spawn` | **NOT cleanly reversible** | kill the spawned process (best-effort); any side effects it already produced persist | not first; promotable only under the no-rollback acknowledgment below |
| `resident_cycle` | **irreversible** | a cycle that ran cannot be un-run | as above |
| `service_restart` | **irreversible** | restarting again is not an undo | as above |

**Rule — two axes, not one (council-corrected).** The council flagged that "irreversible ⇒ `record_only` forever" conflates two independent properties. The pre-commit **veto** (§6) is the *primary* "don't do this" gate; rollback is the *secondary* net for a commit that started and failed. So:
- **Reversibility** governs only whether *rollback is available* after a partial failure.
- **Promotability to live `execute`** requires: (a) the type is **veto-gated** (§6), AND (b) either it is **reversible** (rollback available), OR the operator **explicitly accepts, per-type, that a partial failure is paged-not-undone** (`quarantined`, §6) — a deliberate "this effect can't be un-run, and we govern it anyway" acknowledgment.

`file_write` clears both as reversible → first. An irreversible-but-genuinely-valuable-to-govern type (e.g. `service_restart` behind a real veto) is **not** permanently barred — it needs the explicit no-rollback acknowledgment, not a reversibility it can never have. What stays barred without that sign-off: flipping an irreversible type on *by default*. Enforced via the per-type flag (§12), which is refused at startup for any type lacking either a compensation **or** a recorded no-rollback acknowledgment.

### Crash recovery: content-hash reconciliation, not blind rollback (council-corrected v0.3)

The naïve "on restart, roll back any uncommitted effect" is **wrong** in the exact crash path durable pre-image exists for, and the council found three ways it breaks. The corrected design:

**The decidability problem.** The surface mutation and the Postgres `committed` mark are two non-atomic writes across two systems. A crash *between* them leaves an effect that committed-in-reality but has no mark — blind rollback would then *revert a real commit*. So restart must not guess from the mark alone; it **reconciles against the surface's actual content hash**:

| on restart, current surface hash == | meaning | action |
|---|---|---|
| `payload_sha256` (intended) | apply completed before the mark | **commit-forward** — write the `committed` mark, clear pre-image (resume, do not undo) |
| `pre_image_sha256` | apply never reached the surface | clear pre-image, mark `rejected`/`commit_failed` (nothing to undo) |
| neither | a third party wrote in the gap | **`rollback_failed` → `quarantined`** (§6) — do **not** clobber; page the operator |

This makes the commit boundary decidable for `file_write` (the bytes are the truth, not our mark) and is the load-bearing correctness piece.

**Rollback must re-hold the lease.** Per §4, a dead custodian's leases reap, so by restart time **the lease may be gone**. Restoring a pre-image while holding no lease can clobber a competing writer who acquired the surface in the crash→restart gap. Therefore restart recovery **re-acquires the `required_leases` before touching the surface**; if any is `held_by_other`, it fails closed to `quarantined` rather than restoring stale bytes. Compensation is only "inside custody" when the custody is actually re-established.

**The OTP wiring the existing pattern does NOT give you (reviewer, `lease_supervisor.ex:43`).** `LeaseHolder` children are `restart: :temporary` — the supervisor never restarts them, and a `DynamicSupervisor` starts empty after a node crash. So "EffectCustodian restart re-runs `init/1` recovery" is false under the inherited pattern. Required, and specified here:
- `EffectCustodian` children are **`restart: :transient`** (restart on abnormal exit), not `:temporary`.
- A **boot-time `EffectRecovery` GenServer**, ordered in `application.ex` **before** the HTTP router accepts requests, scans `effects.payloads WHERE rollback_state IS NOT NULL AND committed_at IS NULL` and runs the reconciliation above for each orphan (drains it) before any new effect is accepted. Without this, a full-node crash silently orphans pre-images.
- **Minimum-TTL floor for execute custody.** Because a `file://` lease is a pure DB row reaped at `expires_at` (no in-process renew ticker), proposal time enforces `min(required_leases.ttl_s) ≥ restart_budget + compensation_budget` (operator constants) so the reconciler can re-acquire before the Reaper frees the surface. An execute proposal whose leases are too short to cover recovery is rejected at propose.

Pre-image is captured **into Postgres** at apply-start (a `rollback_state` + `committed_at` alongside `effects.payloads`, §5/§11-item-1), `committed_at` set only after the surface mutation is durable, pre-image cleared on `committed`.

**Idempotency interaction (council blocker).** A rolled-back effect must be **re-executable** — but its `(key, digest)` row persists durably (#1065 §8), and §4 returns the existing `effect_id` for a same-key+digest retry, making the retry a silent no-op. Resolution: on a terminal `commit_failed`/rollback (surface unchanged), the shadow row is **tombstoned** (a `superseded_by`/status marker, not a hard delete — the forensic trail stays) so a same-key+digest re-propose is treated as a *fresh* execution, not an idempotent replay. `quarantined` is **not** tombstoned (the surface is dirty; a blind retry is unsafe — operator first). This resolves the §4/§6 contradiction the council flagged; §4's idempotent-replay rule now reads "…unless the prior attempt is a tombstoned `commit_failed`."

### What this does *not* claim

Rollback is crash/partial-failure recovery within one custody, not a distributed-transaction or saga manager across N surfaces. A multi-surface effect that commits surface A then fails on surface B compensates A's pre-image **only if A's executor is reversible**; if A is irreversible the whole effect is rejected at propose time (an effect may not mix an irreversible commit with a fallible second step). Cross-surface atomicity beyond pre-image compensation is explicitly out of scope for v0 execute.

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
| `commit_failed` | the apply step failed and was **rolled back** (pre-image restored); surface unchanged (§5b). The shadow row is **tombstoned** so the retry re-executes | retry with the **same** key — the tombstone makes §4 re-run it (not an idempotent no-op) |
| `irreversible_effect_type` | an `execute` proposal named an `effect_type` not promotable (§5b: no compensation **and** no recorded no-rollback acknowledgment) — refused before any mutation | use `record_only`, or wait for that type's promotion sign-off |
| `rollback_failed` | apply failed **and** the compensating restore failed (or restart found a third-party write, §5b) — surface is dirty → effect enters terminal **`quarantined`** (§3): lease held, surface unacquirable, operator paged | do **not** retry; operator inspects + force-releases (the one state v0 cannot self-heal) |

**Governance veto interface (`governance_blocked` trigger).** Before committing, `EffectCustodian` calls the UNITARES governance check (HTTP `POST` carrying `effect_id`, `proposer.agent_uuid`, `surface`, `payload_sha256`); a `{"ok": false, "vetoed": true}` (or governance `pause`/`reject` verdict) → `governance_blocked`. The proof is the proposer's `client_session_id`, re-verified server-side (Invariant 1: BEAM consumes proof, never mints it). *(Mode is always explicit in the response and the audit row — v0's "infer execute-mode from the presence of `governance_blocked`" tell is **struck** as a fragile side-effect-as-state pattern.)*

> ⚠ **This endpoint does not exist yet (live-verified 2026-06-25).** No veto/effect-check endpoint is present on the governance MCP (`:8767`); the closest live verdict producer is `dialectic(action="request")`, which returns `{"status": "...", "verdict": "..."}` (not the `{"ok": false, "vetoed": true}` shape above) and **requires an identity binding first**. The veto interface is therefore a **hard build prerequisite for the execute half** — it must be designed against (or built into) the real governance MCP and its actual response shape before any `execute` surface can fire a genuine veto. Do not wire `EffectCustodian` against the invented shape above; finalize it (§11 item 3) by either adding a real `/v1/effect-veto` tool to the governance MCP or adapting the existing verdict path, and update this section to the verified shape.

## 7. Identity (corrected to the live system)

The running tiers are **`strong` / `medium` / `weak`** (`phases.py:71`, `_TIER_SCORES = {"strong": 1.0, "medium": 0.7, "weak": 0.35}` — live-verified). `caller_proven` is a boolean attribute (true when `proof_origin == "caller_asserted"`), **not** a tier. The real `tier == "strong"` write gate is **`phases.py:359`** (v0.1 cited `:345`, which is a `logger.info` format arg — corrected), and today it fires only when the caller passes `require_strong_identity=true` — it is **not yet** an automatic execute-custody gate. **There is no execute-custody tier gate in the running code** (the `execute` branch returns `execute_not_implemented`); §2's "execute gates on `strong`" is therefore a *to-be-built* binding of the existing `:359` strong-check onto the execute path, not an existing one. Re-verified server-side, never trusting a self-asserted claim.

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
- **First `execute` surface → `file_write` class** (vehicle: **lease-plane effect envelope + an in-process `FileWriteExecutor`**). *Revised 2026-06-25 (operator), superseding the 2026-06-18 `agent_spawn` pick below — on blast-radius grounds once §5b made reversibility a first-class axis.*
  - **Why `file_write` first — honest tradeoff, not universal dominance (council-corrected).** It wins *decisively on the one axis that dominates an unproven first commit: blast radius.* `agent_spawn` needs the orchestrator de-inerted — a localhost endpoint that spawns OS processes, RCE-class and irreversible if the bearer leaks; `file_write` is a scoped, reversible `file://` write with no new attack surface. It still satisfies the council's *cheap-but-**contended*** requirement: two effects on the same `file://` lease hit `lease_held` / handoff / single-winner exactly as `agent:/` leases do (live-verified: `file://` is `remote_heartbeat`-routed, DB-CHECK-enforced), so the veto/single-winner membrane is exercised.
    - **What `file_write`-first costs (named, not hidden):** it does **not** reuse the already-live orchestrator vehicle — it needs a new `FileWriteExecutor` + the `effects` schema + the full §5b crash-durable rollback machinery, i.e. it **front-loads the hardest correctness problem (the §5b crash-recovery reconciliation) onto the very first live surface**, whereas `agent_spawn`'s "kill, best-effort" rollback is trivial. And `file://` is a comparatively synthetic membrane vs. the agent/resident coordination the product is really about. The call accepts those costs because a leaked spawn-bearer on an *unproven* first commit is the worse risk; reversibility is a *secondary* benefit to the pre-commit veto (§5b), not the deciding factor.
  - **`agent_spawn` is now deferred, not cancelled.** It remains the natural *second* live-execute surface, but it is the least-reversible class (§5b) and the heaviest security surface (de-inerting `agent_orchestrator` = a localhost endpoint that spawns OS processes, RCE-class if the bearer leaks). It flips on only after `file_write` proves the membrane + rollback end-to-end, and only behind its own separate operator re-confirm (the 2026-06-18 cost acceptance stands but is re-gated).
  - **Original 2026-06-18 rationale (preserved):** agent-spawn satisfied "cheap-but-contended" because the orchestrator already owns `agent:/<id>` leases, supervision, and restart; the accepted cost was the RCE-class spawn endpoint with localhost-only bind + fail-closed `AGENT_ORCHESTRATOR_BEARER_TOKEN`. The §5b reversibility lens is what reordered it behind `file_write`.

A `repo://` surface is **not** promoted to execute by default — if it ever is, reconcile `repo://` against the underlying `file://` lease layer + the gov-plugin per-edit release discipline (the file-lease-leak history) first.

## 11. Gates remaining + still-open

Closes Phase-3 gate (a). Gate (b) — operator names the first effect class — **closed 2026-06-18: both classes, per §10** (content/`doc_update` shadow on the lease-plane envelope; agent-spawn execute on the de-inerted orchestrator). Still required before `elixir/` code: only the **2026-06-24 Wave-3 read** (gate c). With both vehicles chosen, Phase 3 can build two tracks in parallel once the gate opens — the lease-plane envelope (shadow) and the orchestrator de-inerting (execute).

Genuinely open (smaller, deferrable to impl):
1. The `effects.payloads` schema columns + migration slot (assigned at impl time) — now also carries the §5b `rollback_state` for in-flight reversible effects.
2. Exact global payload backstop value and the conservative per-class default.
3. The governance-veto endpoint path/shape (stubbed in §6; finalize when `execute` is built) — **live-verify against the running governance MCP before wiring** (confirm an existing verdict endpoint vs. a new one).

## 12. Execute build sequencing (dry-run-first, blast-radius ordered)

The execute half is built so that **the dangerous capability is the last thing to turn on**, and turns on for one reversible type before anything irreversible. Each step is independently shippable and inert until the next.

1. **Machinery, fully inert.** `effects` schema + migration (`effects.payloads` + `rollback_state`), the `EffectCustodian` GenServer, the `EffectExecutor` behaviour, the governance-veto client, and pre-image capture — all built and tested with **execute still returning `execute_not_implemented`** (the global gate from #866 stays closed; no surface flips). This is the bulk of the code and it commits *nothing*. Tests exercise the custodian against a fake executor that records-but-does-not-mutate.
2. **`FileWriteExecutor`, behind a fail-closed per-type + per-surface flag (default off).** The first type that can flip. Reversible (§5b pre-image), no process spawn. Turning the flag on for a specific `file://` surface is the first moment any governed effect commits. Veto check live; pre-image rollback proven by a fault-injection test (apply that crashes mid-write → surface restored from pre-image).
3. **`repo_commit`**, only after the observed/unobserved guard (§5b) is built — gated separately.
4. **`agent_spawn` / orchestrator de-inert** — last, separately re-confirmed (RCE surface, §10). Irreversible types (`resident_cycle`, `service_restart`) are **not** scheduled for live execute; they stay `record_only` until/unless a compensation story exists (§5b rule).

**Enforcement, not convention — the concrete hook (council-specified).** "Refused at startup" is realized by a `validate_execute_type_flags!/0` called in `application.ex`'s `start_full/0` **before the children list is built**: it reads the per-type execute-flag config and `raise`s (the BEAM refuses to boot) if any enabled type is absent from a **compile-time allowlist** of types that have both a shipped `EffectExecutor` and a registered compensation **or** a recorded no-rollback acknowledgment (§5b). The allowlist starts as `["file_write"]` and grows only as compensations/acknowledgments ship. Because it is a boot-time `raise`, "we flipped on an unpromotable type" is structurally impossible, not merely discouraged — and it's ~10 lines, not new infrastructure. The global record_only path (#1065) is unaffected throughout: record_only shadows keep accruing the promotion evidence base (§7) the whole time.

> **Recovery ordering (from §5b):** the boot-time `EffectRecovery` scanner that drains orphaned in-flight pre-images must be ordered in the supervision tree **after** `validate_execute_type_flags!/0` (config sane) and **before** the Bandit HTTP child (no new effect accepted until orphans are reconciled).
