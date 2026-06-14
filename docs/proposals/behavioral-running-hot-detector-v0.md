# Behavioral "running-hot" detector → orchestrator worker-state read (v0.1, plan)

**Status:** v0.1 plan, parked — pending council. Not an RFC yet — a scoped capture so a fresh (non-hot) session can council + execute. Born 2026-06-03 from a live demonstration during a marathon session. **Blocked (as of 2026-06-14):** unbuilt — gated on step (a) of "Honest scope" below (the behavioral-EISV arm must actually emit signal before the index has anything to read; no running-hot index exists in `src/` yet). Revisit when [[project_eisv-validation-gap]] closes.

## Origin (why this doc exists)

The session's own UNITARES read (`proceed`/`caution`, margin tight, coherence 0.54, risk 0.53) converged with a behavioral proxy (a rising rate of "almost acted on a stale premise, caught only by grounding/council" — four such this cluster). It *looked* like an instrument confirming a behavior. But the read came through `process_agent_update`'s **self-report inputs** (response_text + complexity + confidence), so it was a transform of what the agent declared — **(3) externalize self-report**, not **(4) independent measurement of the agent despite self-awareness.** The "convergence" was largely single-source (the same self-assessment fed both signals).

This doc plans the (4) capability and its first load-bearing consumer.

## Problem

Self-report governance reflects what the agent *declares*. It structurally cannot catch the dangerous failure: **the confidently-wrong agent that reports "fine" while degrading.** An orchestrator supervising worker agents cannot route on worker self-report for exactly this reason — to decide *continue / inject-checkin / pause / reassign* it needs an **independent** read of each worker's state. That independent read is the open frontier already named in [[project_eisv-validation-gap]]: the behavioral / shadow arm, and non-circularity.

The orchestrator and the detector are the same problem from two ends: **the orchestrator is the consumer the behavioral arm exists to serve.** That reframes the behavioral arm from "prove the thesis" to "the orchestrator can't route without it."

## The signal (behavioral, self-report-independent)

A "running-hot index" derived from the worker's **tool stream**, not its check-in text:

- **correction / retry density** — re-reads, "let me check again", reversals after a wrong premise
- **verify-per-progress ratio** — grounding/verification tool-calls per shipped artifact (merge, file write, decision)
- **backtrack rate** — started path X → reversed
- **deliberation length per decision**

Data source: `audit.tool_usage` (already populated — latency_ms, tool_name, agent_id, ts) + the behavioral-EISV arm. **None of these require the agent to self-declare** — that is the whole point.

## The falsifiable test (this is the crux, not a footnote)

The detector earns **(4)** only if it **LEADS** self-declared degradation. Validate on sessions where **self-reported confidence stays HIGH while error-rate climbs**: does the index rise *before* the agent self-observes?

- **Leads** → genuine independent measurement (4); covers the blind spot self-report definitionally can't.
- **Only lags / matches self-report** → (3) with extra steps; kill it.

This session is the **wrong** experiment by construction — the agent self-observed *first* (counted its own misreads), so it cannot separate "caught independently" from "mirrored." A clean test needs the confidence-stays-high-while-degrading case (synthetic, or historical sessions with known-bad outcomes + high stated confidence).

## Orchestrator interface (the consumer)

UNITARES exposes a per-worker behavioral-state read (running-hot index + a confidence on that index) that an orchestrator polls. The orchestrator owns the *policy* (index → action); UNITARES owns the *measurement*. This defines the UNITARES-side contract **any** orchestrator consumes — independent of whose orchestrator it is (a concurrent agent's, or one we design toward; TBD, and the contract is the same either way).

## Honest scope + sequence

1. **The behavioral arm is currently ~97% ODE-fallback / non-participating** ([[project_eisv-validation-gap]]) — the index has *no signal* until that's producing. So sequence: (a) behavioral arm actually emits signal, (b) build the index from the tool-stream features, (c) **validate non-circularity** (the falsifier above), (d) wire the orchestrator read. Do not conflate (a)–(d); each is a separate, separately-falsifiable step.
2. **Load-bearing → council before implementation** (architect + reviewer + live-verifier).
3. **Relation:** thesis is the digital-proprioception paper (Wang 2026c); the (3)/(4) distinction is its crux; this is the (4) build. Tangential to the Wave 3 / substrate work (different surface) but shares the same non-circularity gap, pointed at the *agent* instead of the *substrate*.

## Open questions for council

- Whose orchestrator, built on what? (Doesn't block the UNITARES-side contract, but shapes the consumer interface.)
- Is the running-hot index a new behavioral-EISV channel, or a separate metric beside EISV?
- The non-circularity validation needs labeled "degrading-but-confident" sessions — where do they come from (synthetic harness, or mine historical sessions with bad outcomes + high stated confidence)?
- Privacy/incentive: if workers know they're being behaviorally scored, does it change behavior (Goodhart on the index)? The index is harder to game than self-report, but not ungameable.
