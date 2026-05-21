# Circuit Breaker + Dialectic Recovery

Status: specialized recovery reference. Use for circuit-breaker and dialectic recovery semantics, not as the general architecture overview.

**Last Updated:** 2026-03-22

This system uses a **circuit breaker** to pause agents when risk signals or coherence drop below safe thresholds. Recovery is handled via a **dialectic protocol** that provides a safe path to resume.

This document is the canonical overview for the dialectic flow implemented in `src/dialectic_protocol.py`.

---

## When the Circuit Breaker Triggers

The governance loop evaluates EISV state, coherence, and risk. If the agent enters a high‑risk or low‑coherence region, the system returns a **pause** decision and the agent enters a "paused" or "waiting_input" state.

Common triggers:
- Low coherence (fragmented output or inconsistent work)
- Elevated risk score
- Persistent valence excursion (energy–integrity imbalance)

The circuit breaker is a **protective pause**, not a failure. It exists to prevent runaway behavior and prompt a structured review.

---

## Dialectic Protocol Overview

Dialectic recovery is a structured review process:

1. **Thesis** — paused agent explains what happened and proposes recovery conditions
2. **Antithesis** — counterargument challenging the proposal or highlighting risks
3. **Synthesis** — resolution merging both perspectives: approve, revise, or keep paused

The protocol is implemented in `src/dialectic_protocol.py` and exposed via MCP handlers in `src/mcp_handlers/dialectic/handlers.py`.

Phases:
- `thesis`
- `antithesis`
- `synthesis`
- `resolved` | `escalated` | `failed`

---

## Recovery Paths

### 1) LLM-Assisted Dialectic (single-agent, recommended)

**Use when no peer reviewer is available or for structured self-reflection.**

The dialectic protocol was designed for multi-agent coordination, but ephemeral agents make synchronous peer review impractical. LLM-assisted dialectic uses a local LLM (Ollama) as a "synthetic reviewer" to provide the antithesis perspective.

Key tool:
- `llm_assisted_dialectic` — runs full thesis→antithesis→synthesis using local LLM

~~~python
result = llm_assisted_dialectic(
    root_cause="Agent memory consumption increasing over time",
    proposed_conditions=["Run memory profiler", "Check for circular references"],
    reasoning="Memory leak suspected in state management"
)
# Returns: recommendation (RESUME/COOLDOWN/ESCALATE), synthesis, next_steps
~~~

**Requirements:** Ollama running locally (`ollama serve`)

**How it works:**
1. You provide thesis (root_cause, proposed_conditions, reasoning)
2. Local LLM generates antithesis (concerns, counter-reasoning, suggested modifications)
3. Local LLM synthesizes both perspectives into resolution
4. Result stored in knowledge graph for learning

### 2) Full Dialectic Review (peer-to-peer)

Use when another agent can serve as reviewer.

Key tools:
- `request_dialectic_review` — start a review session (assigns reviewer)
- `get_dialectic_session` — monitor progress by session id or agent id

### 3) Self-Recovery (Tier-1)

For simple stuck scenarios (timeouts, trivial stalls) when the state is safe:

- `self_recovery(action="quick")` — checks coherence/risk/valence and resumes if safe (coherence > 0.60, risk < 0.40)
- `self_recovery(action="review", reflection="...")` — reflective recovery for cases that don't meet quick-resume thresholds

> **Note:** `direct_resume_if_safe` is deprecated. Use `self_recovery` instead.

Recommended conditions:
- short monitoring window
- reduced complexity for a few updates

---

## Suggested Workflow

**Paused agent (thesis):**
1. Inspect state: `get_governance_metrics`
2. Request review: `request_dialectic_review(reason=...)`
3. Provide thesis: explain cause, propose constraints (e.g., “cap complexity to 0.4”)

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

**Dialectic tools:**
- `llm_assisted_dialectic` — single-agent dialectic via local LLM (Ollama)
- `request_dialectic_review` — start peer-to-peer review session
- `get_dialectic_session` — inspect session state and transcript
- `list_dialectic_sessions` — list all sessions with optional filters

**Recovery tools:**
- `self_recovery(action="quick")` — fast path resume when safe (supersedes deprecated `direct_resume_if_safe`)
- `self_recovery(action="review", reflection="...")` — reflective recovery for complex cases
- `mark_response_complete` — use if the agent is simply waiting for input

**LLM delegation tools:**
- `call_model` — direct access to local LLM for custom prompts
- `backfill_calibration_from_dialectic` — optional calibration based on resolved sessions

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
- Default: `gemma3:27b` (fast, good quality)
- Fallback: `llama3:70b` for complex reasoning

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
> **Synthesis:** Self-governance for ephemeral agents isn't impossible, it's *different*. The knowledge graph isn't a substitute self — it's a **coordination substrate**. Coherence metrics measure **trajectory consistency**, not personal continuity.

This reframes the dialectic protocol: it's not about recovering a persistent agent, but about maintaining coherent trajectories across ephemeral instances that share knowledge.

---

## Implementation Notes

The main dialectic protocol is implemented in:
- `src/dialectic_protocol.py` — core protocol and data structures
- `src/mcp_handlers/dialectic/handlers.py` — MCP tool handlers
- `src/mcp_handlers/support/llm_delegation.py` — LLM-assisted dialectic functions
- `src/dialectic_db.py` — PostgreSQL persistence

If you are modifying the protocol, update this document and the tool docs to keep agent guidance aligned.
