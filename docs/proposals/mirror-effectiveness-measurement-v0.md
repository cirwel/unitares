# Mirror effectiveness measurement — v0

Status: proposal / not built. Follow-up from the 2026-06-14 mirror-mode scoping (PR #741, which closed the #583 "reflect, don't advise" seam in `_detect_gaming`). Defines a deterministic, operator-funded-free way to answer "does a surfaced mirror signal actually change agent behavior?" — replacing the current dogfood-only, qualitative tuning loop. **Recommends instrumentation + offline analysis first; no agent-facing behavior change in v0.**

## Why

Mirror mode is the de-facto default response shape for healthy disembodied agents (`src/mcp_handlers/response_formatter.py:100-103`, auto → `mirror` when `health_status == "healthy"` and `not _has_sensor_data`). It emits, per `process_agent_update` check-in:

- gaming / autopilot signals (low-variance complexity or confidence) — `_detect_gaming`, `src/mcp_handlers/updates/enrichments.py:1319`
- a complexity-calibration delta line — `_format_mirror`, `response_formatter.py:264-311`
- a pace / restorative line, an edge reflection, `relevant_prior_work` KG hits, and proprioceptive numbers (`phi`/`coherence`/`risk_score`)

Every one of these was added, tuned, and pruned by **dogfood judgment** — the inline comment trail (2026-04-14, 2026-06-03, 2026-06-10, 2026-06-13) and the #572 / #583 / #603 commits are almost entirely noise-reduction by feel. That curation has produced a genuinely low-noise surface, but the repo has **no evidence that any signal moves an outcome.** Today the signal is computed, rendered into the `mirror` list, and discarded. Nothing records "signal X was shown to agent A at check-in N," so the prerequisite for measuring effect does not exist.

This proposal builds that prerequisite and the analysis on top of it. It deliberately stays inside the operator constraints (CLAUDE.md "No paid LLM API budget"): everything here is deterministic — emission logging + offline metric deltas + a regression-discontinuity read. No model API, no metered call.

## The measurement question, made precise

For a signal type `T` with a defined target metric `m_T` and intended direction `d_T`, surfacing `T` is **effective** if the post-emission trajectory of `m_T` moves in direction `d_T` **more than a matched control that did not see `T`**. The two crisp, fully-numeric cases anchor v0:

| signal `T` | target metric `m_T` | intended direction `d_T` | window |
|---|---|---|---|
| autopilot (flat complexity variance) | variance of `complexity_history` | **up** (agent resumes per-report calibration) | next 5 check-ins |
| complexity-calibration delta | `abs(complexity_divergence)` | **down** (self-report converges toward derived) | next 5 check-ins |

The fuzzier signals (edge reflection, `relevant_prior_work`, pace) are explicitly **out of scope for v0** — their "behavior change" requires NL matching (did the agent cite the surfaced prior work? did it pause?) and they should not gate on a measurement we cannot yet make crisply. They become Phase 2 once the numeric harness is trusted.

## Design

### Phase 0 — emission logging (shadow-emit on every mode)

The single missing primitive. When `enrich_mirror_signals` computes signals, record one row per emitted signal **regardless of the response mode actually selected**:

- key: `(agent_uuid, update_index, signal_type)`
- payload: the value of `m_T` at emission time (the variance / divergence that triggered it), the trigger threshold, and `surfaced: bool` — `true` only when the resolved `response_mode == "mirror"`, `false` otherwise.

The `surfaced` flag is the lever. Agents on `minimal` / `compact` / `standard` compute the *same* signals (the enrichment runs before mode filtering) but never see them — they are the natural **shadow control**. With `b3a31e5` making `onboard` default to `minimal`, a non-trivial population is already mode-split, so this control exists in production without any A/B scaffolding.

Storage: reuse `audit.events` with a dedicated event type (e.g. `mirror_signal.emit`) rather than a new table — same path `_detect_gaming` data already travels, deterministic, no migration slot needed (avoids the single-writer migration surface in CLAUDE.md). Write it through the existing executor-pooled `get_db()` so it stays off the anyio task group (CLAUDE.md "Substrate Tax").

Phase 0 changes **nothing** an agent sees. It is pure instrumentation and is independently shippable / revertible.

### Phase 1 — offline analysis script

A deterministic re-eval in the mold of `scripts/dev/section_129_reeval.py`: read the emission log, join each emission to the same agent's subsequent `complexity_history` / `divergence` from `core.agent_state`, and compute, per signal type:

1. **Surfaced-vs-shadow delta.** Mean Δ`m_T` over the window for `surfaced=true` minus the same for `surfaced=false`. The shadow group corrects for regression-to-the-mean — a flat-variance run drifts back toward normal variance on its own, so the *naive* post-delta overstates effect; the shadow subtracts that baseline drift.
2. **Threshold regression discontinuity.** The `variance < 0.005` cutoff (`enrichments.py:1339`) is a natural experiment: agents just above the line get no autopilot signal, just below get one, and are otherwise similar. An RDD at the threshold gives a second, control-independent estimate of the autopilot signal's effect.

Output is a per-signal verdict: `effective` (delta clears a pre-registered effect size with adequate n), `no_measurable_effect`, or `insufficient_data`.

### Phase 2 — feed the verdict back (operator-gated)

Once Phase 1 reads stably across a representative forward window: a `no_measurable_effect` signal is a candidate for retirement or rewrite, and an `effective` one earns its place. This makes the existing dogfood prune loop **quantitative** rather than vibes-driven. Extending the harness to the NL signals (edge / KG-citation) also lands here. No Phase 2 action is taken on Phase 1 numbers alone — the operator is the gate, same as the merge contract.

## Honest caveats (carry these into any Phase 2 decision)

- **Attribution is partial, not clean.** The shadow control and the RDD each address regression-to-the-mean and confounding, but agent behavior has many inputs; the signal is one. A measured surfaced-vs-shadow delta is *evidence of association under a quasi-control*, not a proven causal effect. This is the same epistemic status the §129 fix settled for ("non-spurious, not complete") and should be labeled as such.
- **Mode is not randomly assigned.** The shadow group self-selects (agents/operators choosing `minimal`, plus the new `onboard` default). If verbosity preference correlates with the kind of agent that calibrates well anyway, the control is biased. The threshold RDD is the hedge — it does not depend on mode assignment. Trust a signal's "effective" verdict most when **both** estimators agree.
- **Goodhart / reflexivity.** Measuring "did variance go up after the autopilot nudge" invites optimizing for variance wiggle. Guard: the *quality* outcome is `trajectory_health`, not the proxy metric — Phase 2 must confirm the proxy moved **and** trajectory health did not degrade, or the signal is just teaching agents to jitter their numbers.
- **Low coverage at first.** Like §129, early windows will be thin (per-agent histories ≥5, mode-split, in-window). A `no_measurable_effect` on small n means "not yet shown to help," not "shown not to help." Pre-register the minimum n before retiring anything.

## Out of scope / follow-ups

1. NL-dependent signals (edge reflection acted-on, `relevant_prior_work` cited in a later check-in/outcome) — Phase 2, needs a deterministic citation match on KG node ids, not a model call.
2. Dashboard surfacing of the per-signal verdict (the dashboard already labels fleet calibration; a "signal effectiveness" panel would live beside it).
3. Whether the proprioceptive numbers (`phi`/`coherence`/`risk_score` top-level) reduce follow-on `get_governance_metrics` calls — a separate, tool-call-count measurement, not a behavior-delta one.
