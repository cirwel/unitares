# Calibration Harness (v1)

A **synthetic calibration fixture** for the UNITARES governance server. It drives
the `confidence → prediction_id → outcome` binding through controlled,
ground-truth-known episodes to prove the **measurement plumbing** works:

- the tactical calibration channel populates and its ECE moves, and
- once injected failures make `bad_rate > 0`, discrimination (AUC) is computable.

It does **not** measure any real agent's calibration — confidences are drawn to
land in a target bin behind a known outcome. Read every number as "the harness
can measure," never "the fleet is well-calibrated."

## Why an isolated instance is mandatory

Tactical calibration is a **global, agent-unscoped, in-process** signal:
`calibration check` returns `tactical_evidence.scope == "global"` and ignores
`agent_id` for the `check` action (`src/mcp_handlers/admin/calibration.py`). The
`by_class` breakdown is *derived* (ephemeral → engaged_ephemeral after ≥3
check-ins), so the harness gets no private bucket there either. Therefore the
synthetic failures this harness injects **cannot be filtered out of the live
fleet's calibration** — they would permanently shift the signal that governs
real agents.

So v1 runs against a **dedicated server bound to `governance_test`**. That gives
the isolated server its own calibration singleton; its global pool *is* the
harness rows. `run_v1` refuses the live ports (`:8767`/`:8766`) unless `--i-know`.

**Defense-in-depth (server-side exclusion).** The grader marks every row
`detail.synthetic_calibration_fixture=true`, and the server now acts on it:
`outcome_events` PERSISTS such rows but skips calibration registration entirely
(`record_prediction` / `record_tactical_decision`). So even if the harness were
accidentally pointed at live governance, its synthetic outcomes cannot poison the
global tactical/strategic channels. The harness verifies this itself — `probe_one`
and the report's SECONDARY check confirm the global channel count does NOT move
(`delta ~ 0`). This is belt-and-suspenders behind the loopback guard, and it makes
the self-marking functional rather than merely forensic.

> `governance_test` is currently 2 migrations behind prod (046 vs 048). 047
> (knowledge CHECK widen) and 048 (un-onboarded check-in floor) do not touch the
> onboard → check-in → outcome → calibration path the harness uses, so 046 is
> sufficient for v1. Bring it current if that changes.

## Run

```bash
# 1) bring up an isolated server against governance_test (own port + token)
DB_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/governance_test \
DB_AGE_GRAPH=governance_graph DB_POSTGRES_MIN_CONN=1 DB_POSTGRES_MAX_CONN=4 \
UNITARES_HTTP_API_TOKEN=calib-harness-test-token UNITARES_MCP_HOST=127.0.0.1 \
UNITARES_DISABLE_AUTO_ONBOARD=1 \
python3 src/mcp_server.py --port 8771 &

# 2) verify the spine end-to-end (single-episode gate)
GOVERNANCE_HTTP_URL=http://127.0.0.1:8771 UNITARES_HTTP_API_TOKEN=calib-harness-test-token \
python3 -m scripts.dev.calibration_harness.probe_one

# 3) run v1
GOVERNANCE_HTTP_URL=http://127.0.0.1:8771 UNITARES_HTTP_API_TOKEN=calib-harness-test-token \
python3 -m scripts.dev.calibration_harness.run_v1 --episodes 200
```

## Design facts pinned against the live API (2026-06-16)

- **0.65 gate, not 0.1.** Tactical calibration only registers a (confidence,
  outcome) pair when `evidence_weight >= GRADE_WEIGHTS[TOOL_OBSERVED] = 0.65`
  (`_MIN_TACTICAL_EVIDENCE_WEIGHT`). Below that the row is silently dropped from
  the tactical denominator. The grader sends `verification_source="external_signal"`
  → grade `externally_verified` → weight `1.0`, and also includes the real
  `exit_code`/`command` in `detail` so the grade is grounded in tool observation.
- **Stated confidence is NOT registered as-is.** `process_agent_update`
  (`src/mcp_handlers/updates/phases.py:635-669`) applies two transforms to the
  stated confidence before it becomes the tactical prediction: (1)
  `apply_confidence_correction` (global calibration adjustment) and (2) a
  **weak-identity cap** `min(confidence, 0.55)` when `identity_assurance.tier ==
  "weak"`. `report.py` therefore bins on `detail.reported_confidence` (the
  registered value), never the stated input.
- **`bad_rate` / `ece` exist** in the response: `per_channel_health.tests.bad_rate`
  and `calibration_guidance.failure_modes.tactical.ece` (the latter has a
  min-sample floor; it is `None` until enough samples).

## What the harness validates (v1.1, after council review of PR #770)

A 3-agent adversarial council reviewed v1 and caught that it validated only the
*binding*, not the *measurement* (outcomes were independent of confidence by
construction). v1.1 closes that gap.

**Binding/registration spine** — stated confidence → `prediction_id` → external
(exit-code) outcome → corroborated tactical row. Sound; the exit-code outcome is
exogenous ground truth (no circularity).

**Calibration measurement** — the outcome is now drawn from a KNOWN curve
(`miscalibration.true_accuracy(c) = clamp(c - gap)`; `--gap`, default 0.2), so
confidence and outcome are coupled by an injected miscalibration. `report.py` bins
on the controlled (stated) confidence and checks that the **recovered ECE matches
the analytic injected ECE** within sampling tolerance, and that **AUC > 0.5**
(confidence now discriminates). Validated live: gap 0.2 → injected ECE 0.186,
recovered 0.223 (|err| 0.037), AUC 0.83. That recovery is the actual
measurement-plumbing proof. The server's tactical channel (registered/capped
confidence) is reported as a SECONDARY view; the divergence between the two is the
cap/corrector finding, not noise.

### The confidence cap — lifted in v2 (`--transport mcp`, default)
Weak-tier agents get `min(confidence, 0.55)` (`phases.py:667-669`), so the 0.6–1.0
bins can't populate. The fix, pinned live: **strong tier requires the MCP transport
AND a `continuity_token`** passed on each call (`session.py:619-642` marks
`continuity_token` → `caller_asserted` → strong). The plain REST `/v1/tools/call`
surface *ignores* the token (it resolves identity by `ip_ua_fingerprint` → weak →
capped); the MCP streamable-HTTP transport honors it. Verified three ways: REST +
body-CSID, REST + `X-Session-ID`, and REST + `continuity_token` all stay weak;
MCP + `continuity_token` reaches strong and removes the cap.

`client_mcp.MCPGovernanceClient` implements this (same interface as the REST
client; the runner/report are unchanged). At n=200, gap 0.2 it recovers the
injected miscalibration across the **full** range — e.g. the 0.8–1.0 bin shows
accuracy 0.73 vs mean confidence 0.92, recovering the ~0.2 overconfidence that was
unreachable under the cap.

Two operational notes:
- **Identity rotation.** A single strong-tier agent accumulating synthetic
  failures gets governance-*paused* mid-run; `run_v1` catches that, rotates to a
  fresh identity, and retries (measurement-neutral — the report bins on stated
  confidence, so identity is irrelevant).
- **Per-call session.** The MCP client opens a short-lived session per call
  (continuity_token is portable proof). Simple and correct; a persistent session
  is a v2.1 perf optimization. Use `--transport rest` for faster capped runs.

## Files

| file | role |
|---|---|
| `config.py` | bins, transport, the 0.65 gate constant |
| `client.py` | thin REST wrapper over `/v1/tools/call` (weak tier, capped at 0.55) |
| `client_mcp.py` | MCP streamable-HTTP client; `continuity_token` → strong tier → cap lifted (v2) |
| `grader.py` | sandboxed subprocess runner → exit-code → external signal |
| `miscalibration.py` | injected curve `true_accuracy(c)=clamp(c-gap)` + analytic injected ECE |
| `episodes/` | `Episode` ABC + `CleanControl` (pass) / `SeededTestFail` (fail) source generators |
| `sampler.py` | stratified confidence-bin slots (outcome drawn in the runner, not here) |
| `runner.py` | per-slot: draw confidence → draw outcome from the curve → check-in → grade → outcome |
| `report.py` | recover injected ECE from stated confidence + AUC; server tactical before/after |
| `probe_one.py` | single-episode live verification (the binding gate) |
| `run_v1.py` | entrypoint; loopback-only guard; `--gap` knob |
