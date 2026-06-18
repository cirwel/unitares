# Governed-Effect Plane — Protocol Contract v0

> **For Hermes:** this is the Phase 2 deliverable of [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md). It is **design-only** and **reviewable**, not an implementation order. No `elixir/` code lands until (a) this contract passes a council review, (b) the operator names the first effect class, and (c) the 2026-06-24 Wave-3 gate read. Read the dossier's Council Amendment first — this contract obeys it.

**Created:** 2026-06-18 · **Status:** Draft v0 — protocol contract, pre-council

---

## 1. What this specifies (and what it does not)

This is the wire + lifecycle contract for a **governed effect**: an agent's *proposal to mutate a surface*, mediated under custody. It specifies the request/response shapes, the lifecycle state machine, the dual `custody_mode`, the typed-error vocabulary, and the three holes the dossier council flagged (idempotency, proposer-crash custody, payload contract).

It does **not** choose the first effect class (operator call, dossier Open Q1), does **not** stand up any runtime, and does **not** decide the vehicle — per the dossier's Phase 2 council note, the chosen effect class picks the vehicle (agent-spawn → `agent_orchestrator`; content effect → lease-plane effect-envelope extension). The schema below is identical across vehicles; only the executor differs.

## 2. Custody modes (the operator's dual-mode decision, made precise)

Every effect declares exactly one `custody_mode`:

| mode | what BEAM does | what BEAM claims | minimum identity tier |
|---|---|---|---|
| `record_only` | receives proposal, validates identity/provenance shape, **may** observe/acquire `required_leases`, assigns a durable `effect_id`, emits typed telemetry | **nothing about the side effect.** The proposer (or some external actor) still executes. This is shadow custody / proposal logging / replay — **not** a commit. | `caller_asserted` or better (advisory) |
| `execute` | owns the bounded payload/command contract, holds `required_leases`, may veto on `governance_blocked`, **performs or delegates the commit** under OTP supervision, emits the terminal committed/rejected/revoked fact | "agents propose, BEAM commits" — the genuine new safety property | `strong` (or `caller_proven`) — see §7 |

**Rhetoric discipline (from the dossier):** "membrane" / "effect custody as control-of-act" language is valid **only** for `execute`. In `record_only`, call it shadow custody or proposal logging. A `record_only` effect that borrows commit language is lying about what happened.

A surface may begin `record_only` and **promote** to `execute` once its payload/idempotency/rollback are specified and the gate read passes. Promotion is a per-effect-class decision, behind a fail-closed flag.

## 3. The effect envelope

```jsonc
// POST /v1/effects
{
  "idempotency_key": "uuid-or-content-hash",   // REQUIRED, proposer-supplied — see §4
  "effect_type":     "file_write|repo_commit|agent_spawn|resident_cycle|service_restart",
  "surface":         "repo://unitares/doc_update | file:///abs/path | agent:/<id> | resident:/<name>",
  "custody_mode":    "record_only|execute",     // REQUIRED, explicit — no default
  "proposer": {
    "agent_uuid":         "...",
    "identity_assurance": "strong|caller_proven|caller_asserted|weak"   // server re-verifies; this is the claim
  },
  "provenance": {
    "harness":             "hermes|claude_code|codex|beam|...",
    "session_id":          "...",
    "verification_source": "agent_reported_tool_result|server_observed|..."
  },
  "payload":         { /* mode-specific — see §5; NOT an opaque ref */ },
  "required_leases": [ { "surface": "...", "ttl_s": 300 } ]
}
```

```jsonc
// 202 accepted
{ "effect_id": "...", "status": "proposed|held|committed|rejected|revoked", "custody_mode": "...",
  "custody_expires_at": "RFC3339" }   // when proposed/held custody self-heals if abandoned — see §4

// 4xx typed (see §6)
{ "error": "schema_invalid|identity_required|insufficient_assurance|lease_held|idempotency_conflict|revoked|governance_blocked|custody_expired",
  "ok": false }
```

## 4. Hole (b): idempotency + proposer-crash custody

**Idempotency.** `idempotency_key` is **required**. A retry with the same key returns the existing `effect_id` and current status — it never mints a second effect or races the surface. This mirrors the lease plane's existing idempotent re-acquire (`repo.ex:8,38`; `surface_registry.ex:54-76` returns `{:ok, lease, :idempotent}` for the same holder). A *different* key proposing the *same exclusive surface* gets `lease_held` (§6), not a silent second custody.

**Proposer-crash-after-202.** A `proposed`/`held` effect has a **custody TTL**, surfaced as `custody_expires_at`. Rules:

- The custody TTL is **independent of** `required_leases` TTL but **bounded by** it: custody cannot outlive the leases it depends on (`custody_ttl ≤ min(required_leases.ttl_s)`). When a required lease expires/reaps, the effect transitions to `custody_expired` (terminal for `proposed`/`held`; never for `committed`).
- **`record_only`:** the proposer is *not* required to heartbeat. Custody self-heals at TTL via the lease plane's `remote_heartbeat` self-healing TTL-row path (#568/#569, #588) — same reaper, no new GenServer. Abandoned shadow proposals reap themselves.
- **`execute`:** custody is held by the executing GenServer (orchestrator `AgentRunner`-style, or the lease-plane envelope holder), **not** the proposer. Proposer crash after 202 does **not** orphan an in-flight execute effect — the custodian owns it through to commit/reject and releases on terminal state (`terminate/2` fast path, TTL backstop). This is the orchestrator's existing lease lifecycle (`init/1` acquire → `terminate/2` release), reused.

## 5. Hole (c): payload contract (mode-specific, no opaque refs)

The dossier's `payload_ref: "opaque ref"` is **rejected** — an opaque ref gives the custodian nothing to verify and the audit trail no forensic value. Replace with a typed, mode-specific contract:

| mode | payload shape | size/redaction |
|---|---|---|
| `record_only` | a **content hash** (sha256 of the intended mutation) + a **redacted human summary string** (max 512 chars). No raw bytes. | hash is the integrity anchor; summary is for the operator UI. |
| `execute` | the **actual bytes/command** the custodian will apply (file content, argv, commit tree-ish) — because the custodian *is* the executor and must hold what it commits. Stored in Postgres, referenced by `effect_id`, never logged raw. | bounded (per-effect-type max, e.g. file_write ≤ 1 MiB); **Invariant 7**: continuity tokens / bearer creds must be scrubbed before storage and never appear in telemetry, audit, or summaries. |

Content-hash continuity: a `record_only` effect that promotes to `execute` must present a payload whose sha256 matches the recorded hash — proving the executed bytes are the ones that were shadowed.

## 6. Typed error vocabulary (complete set for v0)

| error | meaning | caller action |
|---|---|---|
| `schema_invalid` | envelope failed validation | fix and resubmit |
| `identity_required` | no caller proof on an `execute` (or write-class) effect | onboard / bind, resubmit |
| `insufficient_assurance` | identity tier below the effect's `execute` floor (§7) | re-bind at a higher tier or downgrade to `record_only` |
| `lease_held` | a *different* holder owns an exclusive `required_lease` | back off / request handoff (`/v1/lease/handoff/offer`) |
| `idempotency_conflict` | same key, materially different envelope body | the key was reused for a different effect — regenerate |
| `governance_blocked` | UNITARES proof envelope present but governance vetoed the commit (`execute` only) | read guidance; this is the new safety gate firing |
| `revoked` | operator/policy force-released the effect's custody | do not retry without re-proposing |
| `custody_expired` | `proposed`/`held` effect outlived its custody TTL | re-propose |

`governance_blocked` is the only error that **cannot** occur in `record_only` (record-only never commits, so there is nothing to veto). Its presence in a response is itself the signal that this was an `execute`-mode effect.

## 7. Identity floor for `execute` (Open Q4)

`execute` mutates real surfaces, so it requires **`strong`** (or `caller_proven`) assurance — never `weak`/`caller_asserted`/anchor-only. The plane **re-verifies** the proof envelope server-side; it does not trust the `proposer.identity_assurance` claim (Invariant 1: BEAM never mints/launders identity, it only consumes explicit UNITARES proof). `record_only` may accept `caller_asserted` because it makes no commit claim. Sub-`strong` proposals to `execute` get `insufficient_assurance`, not a silent downgrade.

## 8. Durable recording (Open Q3) — recommendation

**Record governed-effect lifecycle to a dedicated `governed_effect_events` stream (or `audit_event`), NOT `outcome_event`.** Reasoning is the #846 lesson generalized: `outcome_event` feeds EISV/prior-state validation, and **Invariant 5** forbids runtime/effect telemetry from masquerading as EISV evidence. Routing effect lifecycle into `outcome_event` would re-contaminate exactly the slice #846 just cleaned. A dedicated stream keeps the operator UI (dossier Open Q6) and forensic trail rich while staying out of the predictive matrix. Every emitted row carries `harness_lane`/`effect_lane` tags (Invariant 4) so the partition is enforced at the source, not just at the analysis filter.

## 9. Relationship to existing surfaces (no double-booking)

- An effect's `required_leases` are **real lease-plane leases** (`/v1/lease/acquire`), not a parallel lock. The effect envelope wraps lease custody; it does not reimplement it. `surface_registry` remains the single-winner authority.
- `execute` custody transfer (e.g. operator reassigning a stuck effect) reuses the lease plane's `handoff/offer`+`handoff/accept` pair rather than inventing a transfer path.
- Revocation reuses `/v1/lease/force-release` + `LEASE_FORCE_RELEASE_TOKEN` — the dossier confirmed this is already live and enforced; the effect plane does not add a second revocation mechanism.

## 10. What stays gated after this contract

This contract closes Phase-3 gate (a). Still required before any `elixir/` code:

- **(b)** operator names the first effect class (Open Q1) → picks the vehicle (agent-spawn → de-inert `agent_orchestrator`; content → lease-plane envelope).
- **(c)** the 2026-06-24 Wave-3 gate read.

Phase 3 (the first `record_only` ExUnit thin slice) may begin once (a)+(b)+(c) hold. `execute` promotion (Phase 4) is a separate fail-closed flag flip on one named low-blast-radius surface — recommended `repo://unitares/doc_update`, the only candidate without existing lease coverage.

## 11. Open decisions for this contract's council review

1. Is a dedicated `governed_effect_events` stream worth the migration vs reusing `audit_event`? (§8)
2. Is `custody_ttl ≤ min(required_leases.ttl_s)` the right coupling, or should execute-custody be allowed to outlive a renewable lease? (§4)
3. Should `record_only` ever be allowed to skip `required_leases` entirely (pure observation), or must it at least *observe* the lease state? (§2)
4. Per-effect-type payload size ceilings (§5) — set here or defer to each effect class's own spec?
