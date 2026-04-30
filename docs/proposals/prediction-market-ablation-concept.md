# Prediction-Market Paper-Trading Ablation: First Clean UNITARES Validation

**Status**: Concept (parked 2026-04-29). Pickup conditions at end.
**Author**: Kenny Wang (concept dialogue with Claude, council review, cross-check from Codex)

## Motivation

UNITARES has not been cleanly A/B-ablated on any substrate. Validation to date has been observational — calibration loops, the §11.6 re-grounding counterfactual, audit-window correlations, Chronicler longitudinal metrics. None of these isolate governance-on vs. governance-off as the independent variable. A first clean ablation has high information value regardless of substrate.

Existing prediction markets (Polymarket, Kalshi) are arguably the most tractable ablation substrate available:

- Binary, externally-settled, uncontroversial ground truth
- Adversary is a competent market — not a benchmark you can teach to or contaminate
- Cheap (paper trading; no capital at risk)
- Pre-registerable; outcomes outside analyst control

Coding-task ablation is not obviously easier — outcomes ("did the PR fix the bug", "did this introduce a regression three weeks later") are squishier than market resolution.

## Claim Under Test

Governance-on lane outperforms governance-off lane on a pre-registered primary metric over a pre-registered window, where outperformance includes risk-reduction value, not just raw P&L.

**Bridge concern (load-bearing)**: calibration-of-self ≠ calibration-against-market in liquid books. The most likely outcome is **null** — UNITARES does not help in efficient markets where edge comes from structural advantages (news latency, liquidity provisioning, illiquid-tail mispricing) rather than self-calibration. A null result is informative, not a failed validation: *"UNITARES helps in domains where calibration is the constraint; liquid prediction markets are not such a domain."* This framing must be pre-registered in the analysis plan to avoid funders misreading null-on-bridge as falsifying-the-framework.

## Design

**Paired ablation** with identical:

- Market data feed (single versioned snapshot per decision; both lanes consume same snapshot ID)
- Starting capital
- Latency assumptions
- Execution simulator rules — fill semantics, fee schedule, slippage model — pre-registered
- Prompts, *except* the governance-on lane includes EISV state in context

**Prompt-confound mitigation**: governance-off lane receives a null-shaped placebo string (same length / structure, semantically null) so the lanes don't differ on raw context length or token diversity. Alternative: explicitly reframe as "EISV-as-context vs. no-context" with the acknowledgment that verdicts/dialectic effects are not isolated from context-injection effects. Pick one before running.

**Per-decision logging**: every decision opportunity logged with market state, decision, governance verdict (if any), action taken (`executed` / `blocked` / `delayed` / `reconsidered`), and post-hoc resolution. This converts the experiment from "compare two P&L curves" into characterizing what governance actually *did* decision-by-decision — preserves causal evidence even on null results.

**Pre-registered metric panel** (no aggregation into a utility function — utility weights re-open analyst-degrees-of-freedom that raw metrics close):

- **Primary**: risk-adjusted return (Sortino) net of fees and slippage over the pre-registered window
- **Secondary** (unaggregated, all reported): max drawdown, CVaR(5%), turnover, intervention frequency, catastrophic-error count, missed-opportunity cost

**Stop / success conditions**:

- **Success**: governance-on beats governance-off on primary metric by ≥ pre-registered threshold AND secondary metrics show no degradation > pre-registered tolerance.
- **Null**: any other outcome. Report findings, kill integration for this regime, document the substrate-mismatch finding.

## UNITARES Surface Requirements (live-verified 2026-04-29)

- ✅ **Verdict gating** — `process_agent_update` returns structured `decision` dict; programmatic circuit-breaker works (`src/mcp_handlers/updates/phases.py:886-892`).
- ✅ **Programmatic dialectic** — `request_dialectic_review` MCP tool callable from code (`src/mcp_handlers/dialectic/handlers.py:388`). Harness agent must NOT carry `autonomous` / `embodied` / `anima` tags or auto-skip fires (`handlers.py:443-457`).
- ⚠️ **Outcome ingestion** — `outcome_event` enforces hardcoded enum (`src/mcp_handlers/observability/outcome_events.py:21-24`). `BAD = {test_failed, tool_rejected, drawing_abandoned, task_failed}`, `GOOD = {test_passed, drawing_completed, task_completed}`, `NEUTRAL = {trajectory_validated}`. Tactical/sequential calibration fires only on `HARD_EXOGENOUS_TYPES`. Prediction resolutions must either masquerade as `task_passed` / `task_failed` (loses semantic fidelity) **OR** ~3-line PR adds `prediction_resolved_correct` / `prediction_resolved_incorrect` to `BAD`/`GOOD_OUTCOME_TYPES` and `HARD_EXOGENOUS_TYPES`. **Recommended: land the small PR before running.**
- ⚠️ **Cadence** — sub-second decision cadence trips loop-detection patterns 1-3 (`src/agent_loop_detection.py:144,162,175`): 2 updates / 0.3s, 3 / 0.5s, 4 / 1.0s. Workable at 5-15s spacing; rapid bursts will pause the agent. Pause-streak patterns (2-3) fire even with `autonomous` tag. Decision pace must respect this.

## Council Findings (preserved verbatim — do not relitigate without re-running)

1. **Calibration may not be the binding constraint** in liquid books. Edge in efficient markets is structural (news latency, liquidity provisioning, niche illiquidity), not self-calibration. UNITARES governs agent-to-self; trading rewards agent-to-market. Mitigation: scope to thinner / longer-tail contracts where consensus is poorly formed, OR accept null result as informative.
2. **Coherence won't fire on wrong-but-internally-consistent theses** — adverse-selection trades will not show EISV degradation. The "EISV front-runs P&L drawdown" claim fails on this class of losses.
3. **Gameable incentives** — language tokens (hedged speech) decoupled from action tokens (aggressive sizing). Calibration signal lives in narrative, position sizing lives in actions; they're decouplable. Optimal policy under reward = P&L is Goodhart-compliant epistemic theater. Mitigation: log stated confidence at decision time as a structured field, not via narrative.
4. **Power floor** — 30-80 resolved positions per 3-month window in current Kalshi/Polymarket activity. Underpowered for spreads <5%; pre-register window length, accept exploratory framing if power is insufficient.
5. **Onboarding friction** — Polymarket requires L2 wallet + EIP-712 signing; Kalshi requires KYC even for sandbox at `demo-api.kalshi.co`. Real one-time cost.
6. **Misread risk on null** — funders may interpret null as falsifying UNITARES rather than the bridge. Mitigation: pre-register the null-is-informative framing in the analysis plan.

## Operational Cost Estimate

- **Build**: ~2 weeks. API auth (Polymarket CLOB + Kalshi REST), market filtering, decision loop, prompt scaffolding, settlement reconciliation, paired logging infrastructure, fee/slippage simulator, P&L accounting, `outcome_event` schema PR (3 lines), pre-registered analysis plan.
- **Ongoing**: monthly babysitting — rate limits, settlement edge cases, market churn, lane-state coordination.
- **Window**: 3-6 months for adequate sample.
- **Fleet load**: +1 long-running process beyond Vigil / Sentinel / Watcher / Steward / Chronicler / Discord-dispatch / Lumen.

## Status: Parked

Concept is well-formed enough to execute. Build cost is non-trivial relative to current solo-founder fleet capacity. Most likely outcome is a null result that is informative but unflattering if misread. Recommend pickup when **any of**:

- v6.9.1 paper has landed and v7 work is unblocked
- Collaborator or grant bandwidth materializes to absorb build / operate cost
- v7 corpus maturity nears (~Q3 2026) — a clean ablation would complement corpus-based v7 work

## Cross-Review

- **Council** (2026-04-29):
  - `dialectic-knowledge-architect` — concept critique (calibration-not-binding-constraint as load-bearing objection, gaming risk, substrate-mismatch steel-man)
  - `feature-dev:code-reviewer` — implementation cliffs (prompt confound, paper-fill semantics, power, API divergence, cache cross-contamination, cadence vs. decision pace)
  - `general-purpose` — UNITARES surface ground-truth (outcome enum REFUTED, verdict gating CONFIRMED, dialectic CONFIRMED, sub-minute cadence REFUTED)
- **Codex** — paired-ablation rigor (per-decision logging, risk-adjusted primary metric, utility-based stop condition). Utility-function aggregation reverted in this spec to unaggregated metric panel to close analyst-degrees-of-freedom.
