# Wave 3 (γ) hybrid — successor to the halted 8-surface handler-dispatch port

**Status:** DRAFT — v0 wide-cut REJECTED (§0a); narrow cut (§0b) REVIEWED by a second council + live
telemetry (§0c) → **recommend SHELVING the BEAM handler port for `process_agent_update`; the
addressable win is in Python (enrichment), not in BEAM.** Successor scope after the (D) state-ownership red-team
**halted** the original `beam-wave-3-handler-dispatch.md` 8-surface port (artifact:
`docs/handoffs/wave-3-state-ownership-redteam-2026-06-28.md`). Operator selected resume shape **(γ)
hybrid**. This is a design to react to, not an implementation plan; it owes a council pass + its own
disconfirmer gate before any code (see §6).

## 0a. Council review (2026-06-28) — the v0 wide-cut + envelope is REJECTED; pivot to the narrow cut

A three-lane design council (security / architecture / live-verifier, independent, against live code)
reviewed §0–§7 below. Verdict: **the wide cut (port the §2 update pipeline wholesale + bridge with a
signed envelope) should not proceed as drawn.** It is self-contradictory against live code and the
envelope introduces avoidable risk. A strictly better seam exists. §0b records the pivot; §1–§7 below
are preserved as the rejected v0 for the record.

**Why the wide cut fails (architecture lane, grounded in `phases.py`):**
- **§3 vs §4.4 are mutually exclusive.** `phases.py` mints PG identity at three sites — `:469`
  `ensure_agent_persisted`, `:1225` `get_or_create_agent`, `:1789` `create_agent` (lazy
  mint-on-first-work). So "PG identity writes STAY Python" (§3) is false if `phases.py` ports to
  BEAM, and the api_key 3-way reconcile (`:1221-1296`) cannot be atomic across the boundary (F4 not
  closed — it is restated as a contradiction).
- **F2 substrate-EARNED exemption is omitted.** The strict gate calls `is_substrate_earned()` +
  `verify_substrate_earned()` mid-handler (`phases.py:343-365`) — the embodied/anchored-resident
  exemption (Lumen), distinct from `peer_pid` `substrate_ok`. A fail-closed BEAM gate without it
  either breaks resident check-ins or calls back into Python identity resolution (re-introducing the
  exact (D) boundary coordination the cut was meant to remove).
- **5c is a real two-writer hazard**, not a deferrable risk: circuit-breaker reads `meta.status`
  (`:438/:639`), cross-agent label scan (`:586-590`), and lost-update on the shared `AgentMetadata`
  record.

**Why the envelope adds avoidable risk (security lane):** shared-secret leak ⇒ fleet-wide attestation
forgery incl. substrate residents (vs kernel attestation, which cannot be leaked); `proof_origin`
fail-open **survives in BEAM-internal paths that carry no envelope** (saga recovery, auto-resolve —
needs an allowlist gate + a BEAM-only-signed internal proof type); the envelope lacks operation-binding
(confused-deputy in saga recovery) and durable nonce tracking (replay within `exp` on BEAM restart).

**Correction — the security lane's dialectic-on-BEAM C3 was OVER-STATED (verified 2026-06-28 against
the live lease-plane).** The claim was "dialectic-on-BEAM writes agent state with no attestation." In
fact BEAM's dialectic ops write **only `core.dialectic_sessions`** (`commit_session_row`: `SET
status, phase, resolution_json WHERE session_id AND status NOT IN ('resolved','failed')`) — they do
**not** write `paused_agent_id`/`reviewer_agent_id` on resolve, never touch `core.agents`/agent-state,
and the agent unpause stays **Python, identity-gated**. The endpoint is bearer-gated (`http_auth.ex`,
all paths) and localhost-only (`127.0.0.1`). So the exposure is "a holder of the bearer with local
access can set an existing session terminal" = the designed gov-mcp↔lease-plane trust boundary, not an
agent-state/identity bypass. **No fix warranted** — an attestation envelope here would be security
theater and would import the C1 shared-secret risk. Recorded so it is not "fixed" wastefully later.

## 0b. The narrow cut — v0.2 PRIMARY DESIGN (council-recommended)

The cut is **identity vs state-compute**, not "front door vs handler." Python keeps everything
identity-bearing and in-process; BEAM runs the substrate-tax-heavy **state estimation** as a
**stateless compute RPC** — value in, decision out, **no identity crosses the boundary, no envelope.**

### 0b.1 The cut line
- **Python (unchanged, in-process):** transport + identity resolution + the strict write-gate +
  `_compute_identity_assurance` + the substrate-earned exemption + ALL PG **identity** writes
  (`core.agents`/`core.identities`, incl. the three lazy-mint sites) + the api_key reconcile.
  Python decides *who* the caller is and *whether the update is authorized* — entirely before any
  handoff. Because authorization happens in-process, BEAM needs no proof of it.
- **BEAM (new):** the state-estimation compute — `run_enrichment_pipeline` + the locked ODE /
  `governance_core` math + the EISV/CIRS derivation + the **`core.agent_states` persist** (STATE,
  not identity). This is the ~96% of the `process_agent_update` floor §5a identified, and the part
  that suffers the anyio↔asyncio tax in Python.

### 0b.2 The RPC contract (no identity, no envelope)
Python, after authorizing, sends BEAM a pure state payload and gets a decision back:
```
Python → BEAM:  { agent_uuid (opaque key, NOT an auth claim), prior_state (EISV + baseline),
                  observation (the update's signals), config_epoch }
BEAM   → Python: { new_state (EISV), verdict (proceed/pause + sub_action), drift/coherence,
                  recommended_status_transition?, cirs_emission? }
```
`agent_uuid` is a lookup key for which state row to compute, **not** a trust assertion — BEAM acting
on a wrong key only mis-routes a *state* computation Python already authorized, never an identity or
privilege decision. Nothing security-bearing is marshalled, so the C1/C2/I1/I2 envelope risks do not
arise.

### 0b.3 Who writes what (single-writer preserved)
| Write | Owner | Why |
|---|---|---|
| `core.agents` / `core.identities` (identity, incl. lazy mint, api_key) | **Python** | identity stays in-process; F4 atomic in one process |
| `agent_metadata` status transitions (e.g. `action=pause`) | **Python (post-step)** | BEAM *recommends* in its decision; Python applies → metadata single-writer, no replica, 5c dissolved |
| `core.agent_states` (EISV), baselines, CIRS events | **BEAM** | the tax-heavy state I/O; not an identity surface, so (D) does not apply |

### 0b.4 Serialization (the §2 critical section)
Two options for the per-agent serialization the StateLockManager provides today:
- **(target) per-agent saga in BEAM** — the **proven dialectic-on-BEAM pattern**: a per-agent
  reserved-slot serializes concurrent updates; no Python lock spans the network call. §2 invariants
  1/3/4/7 (the ones that must collapse to one message) collapse naturally because the compute is one
  BEAM message per update.
- **(interim) Python lock around the RPC** — simplest, but holds the lock across a network round-trip
  (~ms; acceptable at current volume). Ship interim first, move to the saga if contention shows.

### 0b.5 Consequences (each a council concern, dissolved)
- **No envelope** → no shared secret (C1), no cross-boundary fail-open (C2 moot — Python gates
  in-process), no operation-binding/nonce problem (I1/I2), 5b gone.
- **F4 atomic** — identity + api_key writes stay one Python process.
- **F2/F5 moot** — gate, assurance, substrate-earned exemption stay where the contextvars + DB live;
  no callback across the boundary.
- **5c dissolved** — metadata single-writer Python; BEAM holds no metadata replica.
- **Matches the live, proven pattern** — dialectic-on-BEAM is a pure-compute/coordination port, not
  an identity port; the narrow cut applies the same discipline (and reuses the saga infrastructure).

### 0b.6 The (smaller) disconfirmer gate for the narrow cut
- **(D)** — not engaged: identity never crosses the boundary, so there is no state-ownership cutover
  of identity to red-team. Confirm only that no `core.agents`/identity write sneaks into the BEAM RPC.
- **(B)** — re-measure the RPC crossing cost (the state payload is bounded: EISV + observation, no
  full identity marshalling). Budget per this topology.
- **(A.1)/(C)** carry over (A.1 done; C = `anubis-mcp`). **(E)/(F)** unchanged.

### 0b.7 Open questions (v0.2, all smaller than the wide cut's)
- (i) Exact RPC payload for `run_enrichment_pipeline` — enumerate its non-identity inputs (the
  enrichment reads `agent_metadata` for some fields; decide which become payload vs which keep the
  compute Python-side).
- (ii) Interim-lock vs saga-serialization decision threshold (measure contention first).
- (iii) `core.agent_states` schema ownership — confirm no identity columns ride that table.

(Not a γ concern: the dialectic-on-BEAM C3 was investigated and found over-stated — see §0a
correction; no action.)

---

## 0c. v0.2 council + live telemetry (2026-06-28) — recommend SHELVE the handler port

A second council (boundary-integrity / architecture / adversarial) reviewed the §0b narrow cut, and
the decisive `[checkin_phases]` telemetry (n=2169 live check-ins,
`docs/handoffs/wave-3-checkin-phase-telemetry-2026-06-28.md`) was pulled. Both converge: **the narrow
cut does not target the actual cost.**

**Telemetry — where the `process_agent_update` time actually goes** (slowest 1% of check-ins):
`enrichment ~1031ms`, `locked_update ~78ms`, `prepare_unlocked ~40ms`, `post_update ~38ms`. The
**locked compute the narrow cut moves to BEAM is ~78ms**; the **~1s tail is the enrichment pipeline.**

**Council findings against §0b:**
- **Boundary lane:** the slim RPC payload is wrong by ~100×. The real prior-state is the full
  `UNITARESMonitor` (rolling E/I/S/V histories ≥50, behavioral EMA, continuity layer, adaptive
  governor), not `{EISV+baseline}`. Five identity/metadata writes execute *inside* the compute block
  (pause enforcement `meta.status`, `_persist_thread_identity_async`→`core.identities`, the #425 Path-D
  `create_agent`→`core.agents`, `persist_runtime_state`, loop-cooldown). `run_enrichment_pipeline`
  reads ≥6 in-process singletons (`pattern_tracker`, `calibration_checker`, `event_detector`,
  `ACTIVE_SESSIONS`, the dashboard `broadcaster_instance`) → **cannot be a stateless RPC.**
- **Architecture lane:** the atomic seam is **inverted** — the durable state persist already runs
  *outside* the lock today; the atomic-under-lock piece is the *status transition*, which §0b pulls
  out to a Python post-step. That creates an **unsafe-direction crash window** (state row says
  `pause`, `meta.status` stays `active`) with **no reconciler** (existing sweeps only auto-*resume*) —
  a blocker. The `core.agent_states.identity_id` FK forbids cross-runtime atomic identity+state, and
  the #425 recovery mint sneaks an identity write into the BEAM path.
- **Adversarial lane:** the ~96% premise does not map to live code — the anyio↔asyncpg tax is already
  mitigated (ExecutorPool PR #218 + ODE/auth/loop-detect all on `run_in_executor`); the 2751ms
  `audit.tool_usage` p99 vs the 404ms handler-internal p99 means the ~2.3s gap is **event-loop
  queue-wait around the handler**, which a handler port does not remove.

**Recommendation: shelve the BEAM handler port for `process_agent_update`.** The addressable wins are
both in Python and do not need BEAM:
1. **Parallelize the enrichment pipeline** — the independent, fail-safe enrichments with
   `asyncio.gather`; make KG-heavy ones fire-and-forget. Targets the ~1s tail directly.
2. **Profile the 404→2751ms event-loop-queue gap** (likely default-pool saturation) → a dedicated ODE
   thread pool / pool sizing. Targets the substrate-tax gap without a port.

Dialectic-on-BEAM (already shipped — a pure-compute/coordination port, not a state/identity port)
remains the correct use of BEAM. The update handler's bottleneck is simply not what BEAM removes.
§0a/§0b and §1–§7 are preserved below as the design record that led here.

---

## 0. (REJECTED v0 — preserved for the record) Why (γ), in one paragraph

The red-team confirmed the identity middleware is **not** a clean set of movable state surfaces. The
genuine caches/tables (session cache, onboard PIN, transport binding, agent-metadata, the PG
identity tables, the continuity token) **are** movable — the reducibility lane proved per-surface
ETS/GenServer designs. But the **security layer is irreducible under a proxy architecture**:
`peer_pid` is kernel-attested at the socket and **does not survive marshalling**; `proof_origin` is a
cross-surface derived judgment that **fails open** today; and ordering invariants (#945, #802,
injected-CSID) live in straight-line control flow, not in any surface. (γ) keeps that security layer
**in Python at the transport boundary** (where it owns the socket and the attestation) and ports
only the **post-authorization handler execution** to BEAM, with a **signed attestation envelope** as
the trust contract between them.

## 1. The cut line

```
┌─────────────────────────────── Python front door (owns the socket) ───────────────────────────────┐
│  transport (UDS/HTTP)  →  identity-resolution TRANSACTION (all PATHs, one unit):                    │
│     • peer_pid kernel attestation + substrate_claims gate (#802)                                    │
│     • resolution PATH 0/1/2/2.8/3 (sticky cache, session cache, PG mint, token rebind)             │
│     • proof_origin / assurance-tier derivation                                                      │
│     • the "a read must not produce a binding" ordering guard (#945)                                 │
│     • PG identity writes (core.identities, core.agents, substrate_claims, process_binding)          │
│  ── emits a SIGNED ATTESTATION ENVELOPE (§2) ──▶                                                    │
└────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                            │  request + envelope (marshalled)
                                            ▼
┌─────────────────────────────── BEAM authorized-execution plane ───────────────────────────────────┐
│  verify envelope signature  →  FAIL-CLOSED write-gate (consumes envelope, does NOT re-derive)       │
│  →  handler dispatch + the §2 update pipeline (process_agent_update phase chain, ODE/state-estim,   │
│      lock-invariant critical section)  →  non-identity state writes; agent_metadata ETS (read-repl  │
│      + the api_key reconcile co-located here so invariant-1 stays atomic)                           │
└────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Principle:** Python answers "*who is this caller and may they write?*" (cheap, security-bearing,
socket-coupled). BEAM does "*the authorized work*" (substrate-tax-heavy compute + coordination).

## 2. The signed attestation envelope (the linchpin)

The red-team's core finding: you cannot marshal `peer_pid` to BEAM — a copied integer is not a
kernel attestation. (γ)'s fix: **don't marshal the raw transport facts; marshal the front door's
signed *verdict* about them.** Only the socket-owning Python front door can mint a valid envelope.

- **Contents:** `{ agent_uuid, public_agent_id, proof_origin, assurance_tier, caller_proven,
  peer_attested:bool, substrate_ok:bool, csid_origin, iat, exp, nonce }`.
- **Signature:** HMAC-SHA256 over the canonical payload with a Python↔BEAM shared secret (separate
  from the continuity-token secret). BEAM verifies; an envelope it can't verify ⇒ reject.
- **Why this preserves the #802 guarantee:** the security property is no longer "BEAM saw a UDS
  peer" (impossible — BEAM has no socket) but "the front door, which DID the kernel attestation,
  cryptographically asserts `substrate_ok`." Forging it requires the shared secret, not a copied
  header. The attestation's *trust root* moves from the kernel-at-BEAM (unavailable) to
  the-front-door's-signature (available, and the front door still does the real kernel check).
- **Replay/expiry:** short `exp` (seconds) + per-request `nonce` bound into the signature; BEAM
  rejects expired/replayed envelopes. (Open question 5d.)

## 3. Surface disposition (all ~11 from the red-team)

| Surface | Disposition | Note |
|---|---|---|
| `peer_pid` attestation + `substrate_claims` gate | **STAY Python** | socket-coupled; result → envelope `substrate_ok`/`peer_attested` |
| `proof_origin` / assurance derivation | **STAY Python** | derived; result → envelope; BEAM consumes, fail-CLOSED |
| `csid_transport_injected` | **STAY Python** | transport provenance → envelope `csid_origin` |
| #945 no-side-effect-read ordering | **STAY Python** | control-flow guard in the resolution txn |
| resolution PATHs 0/1/2/2.8/3 | **STAY Python** | the transaction is one unit |
| session→UUID cache (C), onboard PIN (F), transport binding (B) | **STAY Python** | used by the resolution txn; not worth splitting from it |
| PG identity tables (D): identities, agents, substrate_claims, process_binding, presence-lease | **STAY Python (writes)** | mint/bind happen in the txn; BEAM reads PG/replica as needed |
| continuity token HMAC (E) | **STAY Python** | minted in the txn; stateless, no reason to move |
| honesty gates (H) | **shared config** | both read `Application.get_env`/env; same flags |
| agent-metadata cache (G) | **PORT to BEAM (read-replica)** | ETS hydrated from PG; **writes (mint/label/api_key) co-located with the update pipeline in BEAM** so invariant-1 stays atomic |
| handler dispatch + §2 update pipeline (phases.py) | **PORT to BEAM** | the substrate-tax-heavy work; the actual win |

## 4. How each (D) trigger is resolved (by construction)

1. **`peer_pid` irreducibility** → not marshalled; the front door's signed `substrate_ok` verdict
   crosses instead. Trust root = signature, not a copied integer.
2. **`proof_origin` fails open** → BEAM's write-gate **defaults to REJECT** when the envelope is
   absent/invalid or `proof_origin` unset. This *fixes* the current fail-open default (`phases.py:336-340`)
   as part of the move — the consumer never silently passes.
3. **#945 / #802 / injected-CSID ordering** → the whole resolution transaction stays in one place
   (Python), so the straight-line guards are not severed across a boundary.
4. **Invariant-1 api_key 3-way atomicity (F4)** → the api_key reconcile + metadata write +
   PG-identity update are **co-located in the BEAM update pipeline** (G's writes move with the
   update, not under a separate owner) → one atomic GenServer message.

## 5. Honest open risks (must be closed before/at the new gate)

- **5a — the win-is-partial risk: MEASURED 2026-06-28, does not materialize.** The substrate tax
  lives in the Python event loop; (γ) leaves identity resolution on it and moves handler compute to
  BEAM. The concern was that resolution might dominate the floor, making the win small. Cross-tool
  decomposition of `audit.tool_usage` (14d) refutes that: identity resolution runs on every tool, so
  a light tool's latency bounds the resolution floor. `process_agent_update` p99 = 2751ms; the
  heaviest identity-path tool `onboard` (full resolution + mint, more than a check-in does) p99 =
  118ms; trivial-handler tools (`identity`, `get_governance_metrics`, `check_calibration`) p99 =
  9–14ms. So identity resolution is ≤~4% of the `process_agent_update` floor; the ~96%+ (the
  StateLockManager critical section + persist + event-loop coordination) is what moves to BEAM. The
  latency win is substantial, not partial. Method/data: `docs/handoffs/wave-3-gamma-5a-floor-split-2026-06-28.md`.
- **5b — the envelope is a new trust primitive.** New shared secret (key management, rotation),
  signature canonicalization (byte-parity hazard, same family as §5.3's signature concern), and a
  new forgery surface if the secret leaks. The front door becomes a single point of authz-trust.
- **5c — metadata cache coherence.** BEAM's ETS metadata is a read-replica of PG written by the
  Python resolution txn (mint/label) AND the BEAM update (api_key/status). Two writers to PG +
  BEAM-ETS needs an invalidation/refresh contract (envelope can carry a metadata epoch, or a notify).
- **5d — envelope replay/expiry** semantics (nonce store? stateless short-exp?) — see §2.
- **5e — boundary-crossing re-budget for (B).** The envelope marshalling is the new per-request
  cost. Lighter than full-identity marshalling (identity is resolved Python-side), but must be
  re-measured against (B)'s ×2/×3 budget for *this* topology, not the original's.

## 6. This re-scope re-opens the disconfirmer gate (smaller)

(γ) is a new scope, so it gets its own §11-style gate, but most disconfirmers are already addressed:
- **(D)** — addressed *by construction* (§4); the gate just confirms the envelope design actually
  preserves #802/#945/proof_origin (a focused re-derivation, not a fresh 11-surface hunt).
- **(B)** — **re-measure** the envelope-crossing cost (§5e) + the §5a resolution-vs-handler split.
  These two are the new load-bearing measurements.
- **(A.1)/(C)** — carry over (A.1 done; C = use `anubis-mcp`).
- **(E)/(F)** — unchanged (operator opportunity-cost; baseline unpinnable).

## 7. Out of scope / sequencing

- Not in scope: moving the transport/socket to BEAM (that's the (β) shape; if §5a says resolution
  dominates the floor, revisit β instead).
- Sequencing: (1) the §5a measurement (resolution-vs-handler floor split) gates whether (γ) is worth
  building at all; (2) then the envelope spec + fail-closed gate as the first slice; (3) then the
  update-pipeline port. Dialectic-on-BEAM (already shipped) is the proof the per-transaction +
  saga + fail-safe-fallback pattern works and is the template for the update-pipeline slice.

---
*Drafted 2026-06-28 from the (D) red-team findings (3 independent lanes). Supersedes the 8-surface
scope of `beam-wave-3-handler-dispatch.md`, which is halted, not deleted (its §2/§5/§7 analysis still
applies to the BEAM-side update pipeline). Owes a council pass before implementation.*
