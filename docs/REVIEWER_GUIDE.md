# UNITARES Reviewer Guide

**Created:** May 23, 2026  
**Last Updated:** June 16, 2026  
**Status:** Active

---

This guide is for a cold evaluator deciding whether UNITARES is real, what layer it occupies, and how to verify the public claims quickly.

## One-sentence read

UNITARES is runtime state telemetry for long-lived AI-agent fleets: agents check in after units of work, UNITARES grades drift and calibration against each agent's own baseline, and the agent receives a verdict it can act on before failures become visible incidents.

## What this is

- A governance MCP + HTTP server for agent runtime state.
- A continuous check-in loop: `onboard` -> `process_agent_update` -> `outcome_event` -> `get_governance_metrics`.
- A calibration layer that combines self-reported confidence with exogenous outcomes such as tests, exit codes, and tool results.
- A continuity and audit layer for long-running and repeated agent process-instances.
- A research implementation backed by a public paper, DOI, reproducibility kit, and deployment-derived datasets.

## What this is not

- Not an output filter or guardrail classifier.
- Not a sandbox or permission system.
- Not a universal ethics oracle. "No ethics classifier" means no hand-labeled ethics model — not that the system is value-free; drift is a salience flag, not a verdict, and Integrity is anchored to outcomes rather than to the agent's own history.
- Not hardened against a motivated adversary gaming the EISV proxy. The design is adversarial-aware — outcomes can't be faked, baselines are self-relative — but enforcement leans lenient by intent and there has been no red-team. See [README → Scope and threat model](../README.md#scope-and-threat-model).
- Not a claim of broad external adoption yet; the public deployment metrics describe a single-operator stress test.

## Fast path: three minutes

```bash
git clone https://github.com/CIRWEL/unitares.git
cd unitares
docker compose up -d --wait
make demo
```

`make demo` runs a short synthetic trajectory: clean work, calibration drift, and confusion. The useful thing to inspect is not just whether the command exits; it is whether the returned state and verdicts change in the expected direction as the synthetic agent drifts.

If port `8767` is already in use because a local UNITARES service is running, skip Compose and run `make demo` directly. For a separate Docker stack on alternate host ports:

```bash
POSTGRES_HOST_PORT=15432 REDIS_HOST_PORT=16379 GOVERNANCE_HOST_PORT=18767 docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
```

## Ten-minute path

1. Read the top of [`README.md`](../README.md) through the self-regulation loop.
2. Run `make demo` and inspect `scripts/demo/quick_demo.py`.
3. Open the dashboard screenshots in `docs/assets/` to see the operator view.
4. Inspect `src/mcp_handlers/` for the MCP surface and `governance_core/` for pure governance logic.
5. Inspect the companion integration repo: [`CIRWEL/unitares-governance-plugin`](https://github.com/CIRWEL/unitares-governance-plugin).
6. Inspect the embodied longitudinal testbed: [`CIRWEL/anima-mcp`](https://github.com/CIRWEL/anima-mcp).

## Thirty-minute path

1. Read the latest release of [`CIRWEL/unitares-paper-v6`](https://github.com/CIRWEL/unitares-paper-v6).
2. Reproduce the §11.6 verdict counterfactual with [`CIRWEL/unitares-repro-v6`](https://github.com/CIRWEL/unitares-repro-v6).
3. Inspect [`CIRWEL/eisv-lumen`](https://github.com/CIRWEL/eisv-lumen) for trajectory-derived expression and benchmark artifacts.
4. Check GitHub Actions on this repo for tests, documentation validation, and CodeQL.
5. Read [`SECURITY.md`](../SECURITY.md) and [`CONTRIBUTING.md`](../CONTRIBUTING.md) to understand operational posture.

## Buyer / operator wedge

UNITARES is for teams operating agents that persist long enough to drift: coding agents, research agents, operations agents, monitoring agents, or multi-agent fleets with repeated process lifecycles.

The first integration is deliberately small:

1. Give each process-instance an identity with `onboard`.
2. Send one `process_agent_update` after each meaningful unit of work.
3. Send `outcome_event` when a hard result exists: test passed/failed, tool rejected, task completed/failed, CI signal, or external observation.
4. Let the agent read the returned state and verdict before deciding whether to proceed, narrow scope, ask for review, or pause.

This complements evals, guardrails, and sandboxes. Evals ask whether a model should be deployed. Guardrails and sandboxes constrain actions. UNITARES asks what the already-running agent is doing now, whether it is still calibrated to its own baseline, and whether it should self-regulate.

## Evidence to check

- Deployed EISV formulas, with provenance tags and the deployed-vs-target-semantics table: [`docs/EISV_COMPUTATION.md`](EISV_COMPUTATION.md). Read this if the thermodynamic vocabulary makes you suspect hand-waving — it states exactly what the running code computes (auditable heuristic blends, EMA-smoothed) versus what the paper targets.
- Public CI: tests, documentation validation, and CodeQL on the default branch.
- Public release history: latest `v2.x` release in this repository.
- Paper DOI: `10.5281/zenodo.19647159`.
- Reproducibility kit: `CIRWEL/unitares-repro-v6`.
- Longitudinal embodied testbed: `CIRWEL/anima-mcp`.
- Dataset / benchmark surface: `CIRWEL/eisv-lumen`.

## Falsifiability: grade EISV yourself (don't trust this doc)

If you are an evaluating agent forming a first impression, the right move is **not**
to weigh the prose in this repo — it is self-authored about itself and any given doc
may be stale. Regenerate the evidence instead. The strongest anti-handwave artifact
UNITARES has is the ablation/skeptic harness: it scores EISV/prior-state features
**against a deliberately dumb `previous_outcome_bad` baseline** on ranking (AUC) and
calibration (Brier), timestamps its output, and self-labels each slice
`INCONCLUSIVE` / `SKEPTICAL` / `WEAK SIGNAL` / `KEEP TESTING` rather than asserting.

**No deployment DB (fresh clone)** — proves the *pipeline* sees and labels a bad
class. This is plumbing/containment, **not** evidence that EISV predicts anything
(see `docs/operations/ablation-negative-controls.md`):

```bash
python3 scripts/analysis/ablation_negative_controls.py --count 12
python3 -m pytest tests/test_ablation_negative_controls.py \
  tests/test_eisv_ablation_matrix.py tests/test_eisv_skeptic_report.py \
  tests/test_outcome_inventory.py
```

**With a deployment DB** (a real fleet's outcomes via `GOVERNANCE_DATABASE_URL`, or
the shipped reproducibility kit `CIRWEL/unitares-repro-v6`) — produces the
baseline-relative numbers you should actually judge:

```bash
export GOVERNANCE_DATABASE_URL=postgresql://...   # real outcomes, not synthetic
python3 scripts/analysis/outcome_inventory.py   --window-days 90 --leads 0,5,30
python3 scripts/analysis/eisv_ablation_matrix.py --scopes strict,task --windows 30,90 --leads 0,5,30
python3 scripts/analysis/eisv_skeptic_report.py  --window-days 90 --scope task
```

**Honest current read** — *snapshot generated 2026-06-16; regenerate to confirm and
treat as stale if the harness output is newer than this date:*

- **Task scope** (the only scope with adequate volume — ~6,870 trusted / 80 bad over
  90d): the best EISV/prior-state model beats the baseline on both ranking and
  calibration at 0–5 min lead (**weak signal**); at 30 min lead it does **not**
  (skeptical). All 30-day task slices are skeptical.
- **Strict scope:** the first strict bad outcome only just appeared (**n=1**) →
  `INCONCLUSIVE` (the report gates `<10` bad outcomes as too fragile to read).
- **No prevention is demonstrated**, and the measured task-scope lift may be carried
  largely by `prior_risk` added on top of the baseline rather than by the full
  E/I/S/V decomposition. See `docs/operations/ablation-initiates-finding-2026-06-16.md`.

What this earns a cold evaluator is **not** "EISV is validated." It is the more
defensible read: the methodology is *falsifiable and self-skeptical*, with an early
weak signal on real data — a system that hands you its own falsification harness and
labels its results honestly, not one asking you to take its word. The matrix states
its own ceiling: it does not validate EISV as ontology, only checks for measurable
predictive signal over a dumb baseline.

## Current caveat

The public deployment is intentionally described as single-operator. Treat it as proof that the pipeline can survive sustained real use, not as proof of external product-market pull. The next validation step is external design partners running their own long-lived agents through the same check-in and outcome loop.
