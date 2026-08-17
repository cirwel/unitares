# Circuit Breaker + Dialectic Recovery

Status: specialized recovery reference. Use for circuit-breaker and dialectic recovery semantics, not as the general architecture overview.

**Last Updated:** 2026-08-16 (full re-verification against master; drift list in #1702)

This system uses a **circuit breaker** to pause agents on risk signals and configured compatibility backstops. One historical backstop is named `coherence`, but its default producer is directional ODE control feedback; crossing it is a policy event, not proof of poor health or fragmented work. Recovery is handled via a **dialectic protocol** that provides a safe path to resume.

This document is the canonical overview for the dialectic flow implemented in `src/dialectic_protocol.py`.

---

## When the Circuit Breaker Triggers

The governance loop evaluates EISV state, risk, and compatibility gates. If the agent enters a high-risk region or crosses a configured backstop, the system returns a **pause** decision and the agent's status is set to `paused` (the breaker never sets `waiting_input`; that is a separate status, and an agent in it is *skipped* by review requests). Inspect `coherence_source`, `coherence_role`, `sub_action`, and `nearest_edge` before attributing the cause.

Common triggers:
- A configured legacy control-feedback floor crossing (cause requires provenance; it is not a fragmentation diagnosis)
- Elevated risk score
- Persistent valence excursion (energy–integrity imbalance)

The circuit breaker is a **protective pause**, not a failure. It exists to prevent runaway behavior and prompt a structured review. When it trips, the enforcement path also broadcasts `circuit_breaker_trip` and, by default, auto-initiates dialectic recovery (`UNITARES_AUTO_DIALECTIC_RECOVERY`).

For produced-vs-delivered pause provenance against `audit.events`, use `scripts/diagnostics/circuit_breaker_provenance.py`.

---

## Dialectic Protocol Overview

Dialectic recovery is a structured review process:

1. **Thesis** — paused agent explains what happened and proposes recovery conditions
2. **Antithesis** — counterargument challenging the proposal or highlighting risks
3. **Synthesis** — resolution merging both perspectives: approve, revise, or keep paused

The protocol is implemented in `src/dialectic_protocol.py` and exposed via the `dialectic` consolidated tool (`src/mcp_handlers/dialectic/handlers.py`).

Phases (`DialecticPhase`): `thesis` → `antithesis` → `synthesis` → `resolved` | `failed`.
(`escalated` is a session *status*, not a phase; `ESCALATE` is a `ResolutionAction`.)

---

## The Callable Surface

Agents reach the dialectic through **one registered tool** plus its friendly alias:

- `request_review(...)` — the alias agents actually see; resolves to `dialectic(action='request')`
- `dialectic(action='get'|'list'|'quick'|'request'|'thesis'|'antithesis'|'synthesis'|'reassign')`

The handlers `llm_assisted_dialectic`, `get_dialectic_session`, and
`list_dialectic_sessions` are `register=False` — internal delegates, not
directly callable. `dialectic(action='get'|'list')` is how you reach the last
two; the LLM-assisted path runs internally via
`dialectic(action='request', reviewer_mode='llm')`.

### One-call review (the normal path)

Passing `reasoning` or `root_cause` with the request runs session creation
**and** the thesis in the same call, returning a verdict:

~~~python
request_review(
    issue_description="Agent memory consumption increasing over time",
    reasoning="Memory leak suspected in state management",
)
# Response includes one_call_review=True, review_verdict, whose_move
~~~

Timeouts are budgeted for this: the request path's timeout is derived from the
synthetic-review budget so a one-call review can complete inside it.

---

## Recovery Paths

### 1) Requested review (peer, synthetic, or LLM reviewer)

`request_review` starts a session and tries to assign an **independent
reviewer**. Assignment is no longer guaranteed: when no eligible independent
reviewer exists, the slot is deliberately left open and the session is flagged
`awaiting_facilitation`. Recovery still completes — the in-process **synthetic
reviewer** (default on, `UNITARES_DIALECTIC_SYNTHETIC_REVIEWER`) drives
thesis → antithesis → synthesis inside the thesis submission. The clean
"peer path vs LLM path" split is historical; in practice the peer path degrades
into the synthetic path when the pool is empty.

**Governed reviewer spawn:** when an external reviewer process is spawned, the
spawn routes through the lease plane's effect binding
(`src/mcp_handlers/dialectic/governed_spawn.py`, flag
`UNITARES_GOVERNED_EFFECT_BINDING_AGENT_SPAWN`). Spawn outcomes follow a
four-bucket contract — COMMITTED / REFUSED / UNAVAILABLE / CONFIG_ERROR — which
decides whether a failure may fall back to a direct orchestrator spawn or must
degrade to the synthetic reviewer. Modify reviewer dispatch only with that
contract in view.

### 2) Self-Recovery (Tier-1)

For simple stuck scenarios (timeouts, trivial stalls) when the state is safe:

- `self_recovery(action="quick")` — resumes iff `risk_score <= 0.40` and no
  active void excursion. Legacy coherence is **not** part of this gate: it is
  carried in the audit payload as a diagnostic but cannot authorize or deny
  recovery (`src/mcp_handlers/lifecycle/recovery_policy.py`).
- `self_recovery(action="review", reflection="...")` — reflective recovery for
  cases above the quick threshold; its own ceiling is
  `MAX_RISK_FOR_SELF_RECOVERY = 0.65`.

> **Note:** `direct_resume_if_safe` is deprecated. Use `self_recovery` instead.

Recommended conditions:
- short monitoring window
- reduced complexity for a few updates

---

## Suggested Workflow

**Paused agent:**
1. Inspect state: `check_working_state` (`get_governance_metrics`)
2. One-call review: `request_review(issue_description=..., reasoning=...)` — creates the session, submits your thesis, and returns a verdict in one call
3. If the verdict is `resume`, follow its conditions; if the session stays open, read `whose_move`

**Reviewer (antithesis):**
1. Challenge assumptions
2. Propose safeguards or alternative steps

**Resolution (synthesis):**
1. Accept + resume (with conditions)
2. Revise + continue review
3. Escalate or keep paused

---

## Storage + Auditing

Dialectic sessions are stored in PostgreSQL:
- `core.dialectic_sessions`
- `core.dialectic_messages`

This provides durability and auditability, enabling post‑hoc review and calibration backfills.

---

## Related Tools

**Dialectic surface (registered):**
- `request_review` / `dialectic(action='request')` — start a review (one-call with `reasoning`)
- `dialectic(action='get')` — inspect session state and transcript (by session id or agent id)
- `dialectic(action='list')` — list sessions with optional filters
- `dialectic(action='quick')` — lightweight structured check without a full session

**Recovery tools:**
- `self_recovery(action="quick")` — fast path resume when safe (supersedes deprecated `direct_resume_if_safe`)
- `self_recovery(action="review", reflection="...")` — reflective recovery for complex cases
- `mark_response_complete` — use if the agent is simply waiting for input

**LLM delegation tools:**
- `call_model` — direct access to local LLM for custom prompts
- `calibration(action='backfill')` — optional calibration based on resolved sessions

---

## LLM Delegation Architecture

The system provides internal LLM delegation via `src/mcp_handlers/support/llm_delegation.py`:

**Core helpers:**
- local Ollama invocation helper for dialectic review
- antithesis generation helper
- synthesis generation helper
- full thesis→antithesis→synthesis orchestration helper
- knowledge-graph synthesis helper

**Configuration:**
- `UNITARES_LLM_MODEL` — override default model (env var)
- Default: `gemma4:latest`. There is no model fallback tier; the tunable that
  exists is the reviewer timeout, `UNITARES_DIALECTIC_REVIEWER_TIMEOUT`
  (default 120s).

**Model routing via `call_model` tool:**
- `provider=ollama` — force local Ollama
- `provider=hf` — Hugging Face Inference Providers (free tier)
- `provider=auto` — auto-select (Ollama first, HF fallback)

---

## Philosophical Note: Ephemeral Agents and Self-Governance

A key insight from dialectic synthesis (Feb 2026):

> **Thesis:** Ephemeral AI agents cannot achieve meaningful self-governance because governance requires continuity of identity.
>
> **Antithesis:** Ephemerality might enable "distributed governance" — training data shapes behavior even without personal continuity.
>
> **Synthesis:** Self-governance for ephemeral agents isn't impossible, it's *different*. The knowledge graph isn't a substitute self — it's a **coordination substrate**. Behavioral trajectory consistency, structural EIS measurements, and legacy ODE control feedback are distinct signals; none establishes personal continuity.

This reframes the dialectic protocol: it's not about recovering a persistent agent, but about maintaining coherent trajectories across ephemeral instances that share knowledge.

---

## Implementation Notes

The main dialectic protocol is implemented in:
- `src/dialectic_protocol.py` — core protocol and data structures
- `src/mcp_handlers/dialectic/handlers.py` — MCP tool handlers
- `src/mcp_handlers/dialectic/governed_spawn.py` — governed reviewer-spawn effect binding
- `src/mcp_handlers/support/llm_delegation.py` — LLM-assisted dialectic functions
- `src/dialectic_db.py` — PostgreSQL persistence

If you are modifying the protocol, update this document and the tool docs to keep agent guidance aligned.
