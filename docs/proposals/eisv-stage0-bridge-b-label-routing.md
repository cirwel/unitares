# EISV Stage-0 population bridge — half (b): routing external labels to baselined residents

_Companion to `eisv-maths-roadmap-v0.md` (§6.3 falsifiability gate, §7 anchor registry, Appendix B disjointness). Half (a) — snapshot EISV onto outcome rows that arrive without one — shipped in PR #1210. This specs half (b): make **baselined resident** agents actually carry **externally-verified** outcomes, so the residual-vs-Φ test has a non-empty join. L2/L3 — needs a deliberate motion, not a silent edit._

## Finding: half (b) is partially built, not absent

`agents/watcher/agent.py:343` (`build_resolution_outcome_args`) already maps a Watcher finding resolution to an `external_signal` outcome attributed to **Watcher's own UUID** — confirmed = good outcome, false-positive dismissal = bad. With PR #1210 the handler now auto-snapshots Watcher's EISV onto that row. So for Watcher, the pipe exists. The disjointness in Appendix B (0 joinable rows, recon 2026-06-25) predates this wiring landing at volume. The remaining work is to make the join actually populate, with the right semantics, across more than one resident.

## The real gaps (the half-(b) work)

1. **Baseline verification (blocking).** A label only yields a *residual* if the attributed UUID has a non-synthetic EISV baseline. Confirm Watcher (and each target resident) has `core.agent_state.synthetic = false` rows — i.e. it checks in often enough to build a Welford baseline. If a resident emits findings but never syncs EISV, the snapshot is null and the row still doesn't join. **Verify first; if absent, ensure the resident check-ins land before anything else.**

2. **Temporal binding (correctness).** Today the outcome snapshots EISV at *adjudication* time (operator resolves hours/days later). The falsifiability hypothesis is "the residual **when the work was produced** predicts whether it was good" — so the snapshot should bind to EISV at **finding-emission**, not resolution. Reuse the existing two-phase prediction registry (`register_tactical_prediction` / `consume_prediction`, `src/monitor_prediction.py`): register a prediction carrying EISV+confidence **when the finding is emitted**; the resolve/dismiss **consumes** that prediction, so the outcome row binds the emission-time residual. Without this, half (a)'s resolve-time snapshot tests the wrong moment.

3. **Breadth across residents (power).** Watcher alone is low-volume. Wire the analogous exogenous channels, each attributed to the baselined resident's UUID, tiered per §7:
   - **Sentinel** finding adjudication (findings already durable in `audit.events`) — `external_signal`.
   - **Dialectic verdicts** — schema already lists these as exogenous (`schemas/core.py:415`); attribute the resolution outcome to the participating resident.
   - **Operator corrections** — `gold` tier (highest informativeness), when Kenny overrides a resident's output.
   Invariant 4 holds throughout: every anchor is exogenous; never the loop validating its own trajectory (`server_observation` stays excluded).

4. **Accrual honesty.** Even fully wired, this is forward-only and low-rate (~hundreds of bad labels, class-uneven). `stage_b_viability.py` will report overlap > 0 first, then a noisy AUC that tightens as labels accrue. Per-class power (esp. rare classes) is a ramp, not a switch — say so; don't claim B's justification before the labels support it.

## Phased plan

- **P1 — verify + measure.** Confirm Watcher/Sentinel UUIDs are baselined; run `stage_b_viability.py` to see whether PR #1210 alone moved overlap off zero for Watcher's recent resolves. Establishes the live floor.
- **P2 — temporal binding.** Register-at-emission / consume-at-adjudication for Watcher (and Sentinel). Smallest correctness fix; makes the residual-at-emission the thing being tested.
- **P3 — breadth.** Sentinel + dialectic + operator-correction channels, tiered.
- **P4 — wire `test_failed`.** The most objective bad anchor (1 event in 79k today); route CI/test failures to the responsible agent.
- **Gate:** B's §6.3 justification is claimable only once `stage_b_viability.py` reports a residual-vs-Φ AUC margin with adequate (per-class) power — and a clean negative result is a valid, publishable outcome.

## Non-goals

No suspicion/trust gating on the live path (§7a — informativeness, not intent). No backfill (emission-time state is gone for past rows). No new table — `audit.outcome_events` + the prediction registry already carry this.
