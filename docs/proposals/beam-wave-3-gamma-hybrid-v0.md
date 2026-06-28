# Wave 3 (γ) hybrid — successor to the halted 8-surface handler-dispatch port

**Status:** DRAFT — v0 wide-cut REJECTED by design council 2026-06-28 (see §0a); active direction is
the **narrow cut (§0b)**, which owes a v0.2 redraw. Successor scope after the (D) state-ownership red-team
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
needs an allowlist gate + a BEAM-only-signed internal proof type); **dialectic-on-BEAM is LIVE today
writing agent state with no attestation** and must be covered; the envelope lacks operation-binding
(confused-deputy in saga recovery) and durable nonce tracking (replay within `exp` on BEAM restart).

## 0b. The narrow cut (v0.2 direction — council-recommended)

Port **only the state-estimation critical section** (`run_enrichment_pipeline` + the locked
ODE/persist compute — the ~96% the §5a measurement identified as the substrate-tax prize) as a
**stateless compute RPC**: Python sends the state-vector inputs, BEAM returns the decision +
state-update; **Python applies the resulting writes** (status transitions, `core.agent_states`
persist) as a post-step. Identity resolution, the strict gate, all PG identity mint, and the api_key
reconcile **stay in Python, in-process.**

Consequences (each a council concern, dissolved):
- **No envelope.** Identity facts never cross a trust boundary → no shared secret (kills C1), no
  fail-open-across-boundary (C2 moot for identity — Python gates in-process before the handoff), no
  operation-binding/nonce problem (I1/I2). 5b gone.
- **F4 stays atomic** — PG-agents + metadata + ctx api_key writes remain one Python process.
- **F2/F5 moot** — gate, assurance computation, substrate-earned exemption stay where the contextvars
  and DB live; no callback across the boundary.
- **5c shrinks to nothing** — metadata stays single-writer Python; BEAM holds **no metadata replica**
  (value-in → decision-out).
- **Matches the proven pattern** — dialectic-on-BEAM (already live) is itself a pure-compute/coordination
  port, not an identity port. The narrow cut applies the same discipline.

Open questions for the v0.2 narrow-cut design (smaller than the wide cut's): (i) exact RPC boundary —
which of the locked block's PG writes move to BEAM vs stay Python post-step (state writes can move;
identity/api_key writes must not); (ii) whether the StateLockManager critical section's serialization
moves to BEAM (the dialectic saga pattern is the template) or stays a Python lock around a BEAM
compute call; (iii) the §2 lock-invariants 1/3/4/7 that must collapse to one message — re-evaluate
under the narrow cut. The dialectic-on-BEAM attestation gap (security C3) is a **separate, already-live
issue** to fix regardless of the γ outcome.

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
