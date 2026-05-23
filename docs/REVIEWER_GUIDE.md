# UNITARES Reviewer Guide

**Created:** May 23, 2026  
**Last Updated:** May 23, 2026  
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
- Not a universal ethics oracle.
- Not a claim of broad external adoption yet; the public deployment metrics describe a single-operator stress test.

## Fast path: three minutes

```bash
git clone https://github.com/CIRWEL/unitares.git
cd unitares
docker compose up
# In another shell:
make demo
```

`make demo` runs a short synthetic trajectory: clean work, calibration drift, and confusion. The useful thing to inspect is not just whether the command exits; it is whether the returned state and verdicts change in the expected direction as the synthetic agent drifts.

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

- Public CI: tests, documentation validation, and CodeQL on the default branch.
- Public release history: latest `v2.x` release in this repository.
- Paper DOI: `10.5281/zenodo.19647159`.
- Reproducibility kit: `CIRWEL/unitares-repro-v6`.
- Longitudinal embodied testbed: `CIRWEL/anima-mcp`.
- Dataset / benchmark surface: `CIRWEL/eisv-lumen`.

## Current caveat

The public deployment is intentionally described as single-operator. Treat it as proof that the pipeline can survive sustained real use, not as proof of external product-market pull. The next validation step is external design partners running their own long-lived agents through the same check-in and outcome loop.
