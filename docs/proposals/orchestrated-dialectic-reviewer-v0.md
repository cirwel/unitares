# Orchestrated Dialectic Reviewer — the agent-orchestrator's first consumer (v0)

- **Status:** Design-first sketch (2026-06-23). No code yet. Proposed as the **first real consumer** that de-inerts the BEAM agent-orchestrator — the answer to the 2026-06-24 gate's "Call A" (see `2026-06-24-wave-3-gate-framing.md`).
- **Why this one:** it converts a standing governance weakness (rubber-stamp reviews) into a fix using *exactly* what the orchestrator already does (spawn → lease-bind → supervise → capture exit → clean up). It's demand-real, not hypothetical.

## The problem it fixes

The dialectic engine's reviewer independence is structurally broken, three ways (all in `src/mcp_handlers/dialectic/handlers.py`):

1. **Verdict is hardcoded.** The synthetic-reviewer path builds its synthesis with `agrees=True` (lines 1206/1216/2109/2119). The reviewer *cannot* disagree — so a "review" can never block. The model (gemma4, heterogeneous per #563) generates an antithesis, but the resolution asserts agreement regardless.
2. **No process independence.** `generate_synthesis()` runs **in the same process** as the paused agent's governance call, under a shared `SYNTHETIC_REVIEWER_ID`. There is no second, independently-accountable actor — just a function call.
3. **Shared credential.** The synthetic path borrows the paused agent's `api_key` (handlers.py ~1223) to finalize. The "reviewer" isn't even a distinct identity.

A reviewer that runs in your process, under your-ish identity, and is hardcoded to agree is not a check — it's a ceremony.

## The design

The dialectic engine owns the **verdict semantics**; the orchestrator owns the **lifecycle**. They meet at two existing seams, so almost no new contract is invented.

### 1. Spawn contract (dialectic engine → orchestrator)

When a session needs a reviewer and no live peer agent is available — today's fork to the synthetic path — instead call the orchestrator's HTTP control surface (`elixir/agent_orchestrator/lib/agent_orchestrator/http_router.ex`, #799):

```
POST /v1/agents
  { "cmd": "python", "args": ["-m", "unitares.dialectic_reviewer", "--session", "<session_id>"],
    "lease": { ... } }     # orchestrator already acquires-on-spawn + releases-on-exit
  Authorization: Bearer <orchestrator token>
```

- The orchestrator **lease-binds** the reviewer (proven live: `acquire→run→release` round-trip), **supervises** it (OTP), and on exit returns `{exit_status, output, lease_id, lease_released}` via `POST /v1/agents/:id/await`.
- **Lock it down:** the orchestrator's own docstring calls `/v1/agents` "an authenticated RCE surface." Set `:cmd_allowlist` to the reviewer runner *only*. The dialectic engine is the sole caller; localhost bearer + allowlist is the trust boundary.

### 2. The reviewer runner (new, small)

A standalone process — `unitares.dialectic_reviewer` — that:

1. **Onboards as its own identity** (strict is now live): `start_session(force_new=true, parent_agent_id=<engine/driver uuid>, spawn_reason="dialectic_reviewer")`. No borrowed `api_key`; no `SYNTHETIC_REVIEWER_ID`. The orchestrator already provisions parent-lineage + `server_url` env (#648/#650).
2. **Reads the thesis** from the session via the existing dialectic read path.
3. **Calls the heterogeneous local model** (gemma4 via the existing #563 structured-reviewer call — **local Ollama, no paid API**, per the operator constraint) for a *genuine* structured verdict: `{ agrees: bool, root_cause, proposed_conditions, reasoning }`. The model logic already exists; this just runs it in a process and trusts its `agrees`.
4. **Submits via the dialectic protocol** — `dialectic(action=submit_*)` → `session.submit_synthesis` / `pg_add_message` with the **model-derived `agrees`** (which may be `False`). This is the *same* write path a human reviewer uses (handlers.py:1631–1696); `agrees=False` is already a first-class, supported outcome (line 1657).
5. **Exits.** The orchestrator reaps it and releases the lease.

### 3. How the verdict flows back

**Through the governance DB, not the pipe.** The reviewer's verdict lands as a real dialectic message row via the protocol; the engine's existing resolution logic (`finalize_resolution`, the #564 auto-resolve timer) consumes it exactly as it would a peer agent's. The orchestrator's `output`/`exit_status` are used only for **liveness/diagnostics**, never for the verdict. Clean separation: the orchestrator never has to understand dialectic semantics.

### 4. Retiring `agrees=True`

- Gate behind `DIALECTIC_REVIEWER_MODE = synthetic | orchestrated` (default `synthetic`; flip per-measurement).
- `orchestrated` spawns the real reviewer. The hardcoded-`agrees=True` synthetic path is kept **only as an explicit, labeled fallback** for when the orchestrator or the local model is unavailable — and logged as a degraded "could not obtain an independent verdict," not silently presented as a review.
- **Falsifiable success metric:** does an orchestrated reviewer *ever* return `agrees=False`? The current path returns `False` exactly zero times by construction. A single genuine disagreement is proof the independence is real, not ceremonial. Measure the `agrees=False` rate over N sessions; if it stays 0, the reviewer model isn't actually adding signal and that's its own finding.

## What it takes (the concrete first move)

| Piece | Effort | Notes |
|---|---|---|
| `unitares.dialectic_reviewer` runner | **M** | Reuses #563's gemma4 structured-reviewer call; new = onboard + submit-via-protocol + exit |
| Dialectic→orchestrator spawn call (flagged) | **S** | One HTTP POST at the reviewer-fork; bearer + `cmd_allowlist` |
| `DIALECTIC_REVIEWER_MODE` flag + `agrees=False`-rate metric | **S** | Default-off; measurement is the gate |
| Verdict return path | **none** | Already exists (protocol + PG) |

No new BEAM code (the orchestrator is built+proven), no new verdict contract, no paid API.

## Blast radius / cautions

- **Strict identity is live (2026-06-22).** The reviewer is a tokenless new process until it onboards — it *must* `force_new` + declare lineage + land a real `sync_state`, or strict will (correctly) refuse it. This is the standard subagent-onboarding discipline, not a new risk.
- **RCE surface:** `cmd_allowlist` must pin the spawnable command to the reviewer runner. Non-negotiable.
- **Independence is now two-axis, not total:** gemma4 already gives model-heterogeneity; this adds process + identity independence and removes the shared-credential coupling. It does *not* claim adversarial independence — a same-host local model is still a weak reviewer; the value is that it can now *block*, and that its verdict is independently accountable.

## Relationship to the gate

This is Call A's first consumer. Wiring it de-inerts the orchestrator on real demand (not "architectural coherence"), at which point `monitor-delegated-liveness` and `orchestrator-vouched-identity` have a non-empty population and proceed on their own gates. It is independent of Call B (the `fcntl`→advisory-lock latency falsifier).
