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

## What v1 validates — and what it does NOT (council review, PR #770)

A 3-agent adversarial council reviewed this. The honest scope:

**v1 validates the BINDING/REGISTRATION spine** — stated confidence → `prediction_id`
→ external (exit-code) outcome → corroborated tactical row → read-back. That path
is sound and well-built; the exit-code outcome is exogenous ground truth (no
circularity).

**v1 does NOT yet validate the calibration MEASUREMENT.** The sampler assigns
pass/fail by *position* (`i < n_fail`) and draws confidence *independently* by
bin, so **outcome and confidence are statistically independent by construction**.
There is no injected miscalibration for ECE/AUC to recover — so AUC ≈ 0.5 is
*expected* (any deviation is a sampler artifact), and the ECE is dominated by the
0.55 cap + corrector fixed point + the constant per-bin fail ratio, not by a
calibration relationship. Printing these as "calibration" without that caveat was
a self-deception seam; `report.py` now states it inline.

### The v1.1 fix (the real work item)
Make the **outcome a function of confidence** with an injectable miscalibration
knob: draw pass/fail with probability `f(drawn_confidence; bias)`. Then ECE/AUC
become *recoverable ground truth* — "I injected calibration error X; did the
channel report ≈ X?" — which is the actual measurement-plumbing test. Until then,
v1 is a binding smoke test, not a calibration validator.

### The confidence cap (v1 ceiling)
Weak-tier agents get `min(confidence, 0.55)` (`phases.py:667-669`), so the 0.6–1.0
bins don't populate. **Reaching strong tier requires the MCP transport** — that is
confirmed (a council agent on the MCP transport logged at strong/1.0). The plain
REST `/v1/tools/call` surface this harness uses does **not** reach strong in
testing: echoing `client_session_id` in the body and sending an `X-Session-ID`
header both yield `proof_origin: unknown` → weak → capped (verified live, three
ways). So the v2 lever stands: talk to the server over the MCP transport with
caller-proven binding. (Earlier wording called the cap "structural to REST"; more
precisely it's structural to *every REST handshake tested* — strong is confirmed
only on the MCP transport.)

Secondary lever: accept that tactical calibration scores the EISV-corrected
confidence and frame v1.1 as "is the governance system's *own* confidence
well-calibrated" — but that still needs the v1.1 knob to have any ground truth.

## Files

| file | role |
|---|---|
| `config.py` | bins, fail-ratio, transport, the 0.65 gate constant |
| `client.py` | thin REST wrapper over `/v1/tools/call` (onboard/check-in/outcome/calibration) |
| `grader.py` | sandboxed subprocess runner → exit-code → external signal |
| `episodes/` | `Episode` ABC + `CleanControl` (pass) / `SeededTestFail` (deterministic fail) |
| `sampler.py` | stratified (bin × pass/fail) plan + overconfidence cell |
| `runner.py` | per-episode check-in → grade → outcome, binding via `prediction_id` |
| `report.py` | ECE + AUC from per-agent DB rows; server tactical before/after |
| `probe_one.py` | single-episode live verification (the build-order gate) |
| `run_v1.py` | entrypoint; prod-URL guard |
