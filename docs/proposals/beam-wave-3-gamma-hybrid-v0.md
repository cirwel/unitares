# Wave 3 (γ) hybrid — successor to the halted 8-surface handler-dispatch port

**Status:** DRAFT v0 (2026-06-28). Successor scope after the (D) state-ownership red-team
**halted** the original `beam-wave-3-handler-dispatch.md` 8-surface port (artifact:
`docs/handoffs/wave-3-state-ownership-redteam-2026-06-28.md`). Operator selected resume shape **(γ)
hybrid**. This is a design to react to, not an implementation plan; it owes a council pass + its own
disconfirmer gate before any code (see §6).

## 0. Why (γ), in one paragraph

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

- **5a — the win may be partial (the load-bearing measurement).** The substrate tax lives in the
  Python event loop. (γ) leaves **identity resolution** (Redis + PG lookups) on that taxed loop and
  moves only handler compute to BEAM. (A.1) showed governance_core *math* is 0.8% of the p99 floor —
  but identity-resolution I/O is part of "the rest." **New measurement required:** what fraction of
  `process_agent_update` p99 is identity-resolution (stays taxed) vs handler/update compute (moves to
  BEAM)? If resolution dominates, (γ)'s latency win is small and "shelve" may beat it.
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
