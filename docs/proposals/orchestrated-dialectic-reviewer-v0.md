# Orchestrated Dialectic Reviewer — the agent-orchestrator's first consumer (v0)

- **Status:** Implemented behind opt-in gates. The standalone reviewer, governed-first spawn path, real disagreeing verdict, local/Codex/Claude backend routing, fallback behavior, and provenance tests are present. Operator rollout remains separate from merge.
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
2. **Receives the thesis and server-captured pause evidence** in the bounded orchestrator spawn payload; it does not borrow the paused agent's session or credentials.
3. **Calls the operator-selected heterogeneous backend** for a *genuine* structured verdict: `{ agrees: bool, root_cause, proposed_conditions, reasoning }`. `UNITARES_DIALECTIC_REVIEWER_HOST` selects `local`/`ollama` (default), `codex`, `claude`, or `external` (alias `gemini`). Local uses `UNITARES_LLM_MODEL` (default `gemma4:latest`). Claude may be pinned with `UNITARES_DIALECTIC_CLAUDE_MODEL`; otherwise its authenticated CLI/operator default selects, and the exact models actually used are read from the CLI's `modelUsage` envelope. Codex currently uses its CLI/operator default and records the family when the CLI does not expose an exact identifier. `external` is the **third-family seam**: an operator-configured OpenAI-compatible endpoint (`UNITARES_DIALECTIC_EXTERNAL_BASE_URL` + `_MODEL` + `_API_KEY_ENV`, the last naming the variable that holds the key so no secret rides in a flag value), which reports the provider's own `model` and token usage as provenance. The vendor is configuration, not a code branch — the reason #66/#80 deleted the previous hardcoded `gemini` provider was that nothing wired its key, so it could only answer MISSING_CONFIG.
4. **Submits via the dialectic protocol** — `dialectic(action="antithesis")` followed by `dialectic(action="synthesis")`, with the **model-derived `agrees`** (which may be `False`). This is the same write path a peer reviewer uses; `agrees=False` is a first-class, supported outcome.
5. **Exits.** The orchestrator reaps it and releases the lease.

### 3. How the verdict flows back

**Through the governance DB, not the pipe.** The reviewer's verdict lands as a real dialectic message row via the protocol; the engine's existing resolution logic (`finalize_resolution`, the #564 auto-resolve timer) consumes it exactly as it would a peer agent's. The orchestrator's `output`/`exit_status` are used only for **liveness/diagnostics**, never for the verdict. Clean separation: the orchestrator never has to understand dialectic semantics.

### 4. Retiring `agrees=True`

- Gate process independence behind `UNITARES_DIALECTIC_ORCHESTRATED_REVIEW` (default off) and select the model backend independently with `UNITARES_DIALECTIC_REVIEWER_HOST`.
- `orchestrated` spawns the real reviewer. The hardcoded-`agrees=True` synthetic path is kept **only as an explicit, labeled fallback** for when the orchestrator or the local model is unavailable — and logged as a degraded "could not obtain an independent verdict," not silently presented as a review.
- **Falsifiable success metric:** does an orchestrated reviewer *ever* return `agrees=False`? The current path returns `False` exactly zero times by construction. A single genuine disagreement is proof the independence is real, not ceremonial. Measure the `agrees=False` rate over N sessions; if it stays 0, the reviewer model isn't actually adding signal and that's its own finding.

## What it takes (the concrete first move)

| Piece | Effort | Notes |
|---|---|---|
| `agents.dialectic_reviewer` runner | **done** | Onboards, selects local/Codex/Claude, submits through protocol, records backend/model provenance, exits |
| Dialectic→orchestrator spawn call (flagged) | **done** | Governed-effect first, direct authenticated orchestrator only for availability/config fallback buckets |
| Backend selection + `agrees=False` behavior | **done** | Selection is operator config; verdict is model-derived and parse failures conservatively disagree/fallback |
| Verdict return path | **none** | Already exists (protocol + PG) |

No new verdict contract or model API key is required. Codex/Claude use the operator's authenticated subscription CLIs. Provider-reported cost is preserved when available; "subscription-backed" is not represented as "zero cost."

## Selection and audit contract

- **Who reviews:** this is protocol ownership, separate from model choice. Explicit `self` mode assigns the paused agent. `auto` can choose a standing peer only when `UNITARES_AUTOSELECT_REVIEWER=1`; it excludes the paused/excluded IDs, known non-reasoning residents, candidates known to be non-active or more than 24 hours stale, agents in another live session, and either direction of a reviewer pair used in the last 24 hours. Missing or unparsable status/recency metadata is treated as unknown rather than an automatic rejection. Eligible candidates are ranked by the authority-score inputs that are available (unknown health/history is neutral) and selected with weighted randomness from the top five. Manual/failed-auto leaves the slot open for an explicit assignment or first responder. After a thesis reaches an open slot, `UNITARES_DIALECTIC_ORCHESTRATED_REVIEW=1` summons the independent reviewer process; dispatch/model failure falls back to the labeled in-process synthetic path when enabled.
- **Which backend reviews:** `UNITARES_DIALECTIC_REVIEWER_HOST` chooses `local`, `codex`, or `claude`. An absent value chooses local Ollama.
- **Which model:** local is `UNITARES_LLM_MODEL`; Claude is `UNITARES_DIALECTIC_CLAUDE_MODEL` when set and otherwise the CLI/operator default. Claude can report more than one actual model (for example a primary model plus an internal helper); every exact ID is retained and `model_used` is deliberately left unset when the provider reports multiple models. Codex currently records its family unless its output yields an exact identifier.
- **How failure behaves:** a missing CLI, timeout, nonzero exit, or unparsable host verdict falls back to local Ollama. The fallback source is recorded; no failed external backend silently becomes an approval.
- **What is durable:** the reviewer identity's `model_type` fingerprints the backend/models and its real governance check-in records backend, host, exact model IDs, fallback source, and provider cost when available. The dialectic verdict itself still flows through the ordinary antithesis/synthesis rows.
- **Off-record consultation:** `delegate_inference(host_id="claude:host-adapter", ...)` lets an onboarded Codex/other agent ask Claude for attributed tool evidence without opening a dialectic session. It uses safe mode, disables tools and session persistence, and returns hashes, usage/cost, exact model IDs, latency, requester UUID, and orchestrator execution ID.

## Blast radius / cautions

- **Strict identity is live (2026-06-22).** The reviewer is a tokenless new process until it onboards — it *must* `force_new` + declare lineage + land a real `sync_state`, or strict will (correctly) refuse it. This is the standard subagent-onboarding discipline, not a new risk.
- **RCE surface:** `cmd_allowlist` must pin the spawnable command to the reviewer runner. Non-negotiable.
- **Independence is multi-axis, not total:** model, process, and identity can now be heterogeneous and the reviewer can block. A configured subscription CLI still shares the operator/host trust domain, so this does *not* claim adversarial independence.

## Relationship to the gate

This is Call A's first consumer. Wiring it de-inerts the orchestrator on real demand (not "architectural coherence"), at which point `monitor-delegated-liveness` and `orchestrator-vouched-identity` have a non-empty population and proceed on their own gates. It is independent of Call B (the `fcntl`→advisory-lock latency falsifier).
