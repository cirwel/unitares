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

## Known limitation (v1 ceiling) & v2 levers

**REST-onboarded agents are weak-tier, so confidence is capped at 0.55.** The
REST surface binds identity by server inference (`caller_proven: false`,
`proof_origin: server_inferred`) — it cannot reach `strong` even when
`continuity_token` and `client_session_id` are passed (the #425 REST transport
injects a synthetic session id). Weak tier triggers the `min(confidence, 0.55)`
dampener, so **the 0.6–1.0 confidence bins are structurally unreachable over
REST**, and the calibration the harness measures is of the *corrected, capped*
confidence — not raw stated confidence across the full range.

Verified live: stated `0.93` → registered `0.55`; stated `0.138` → registered
`0.18`. v1's success criteria (bad_rate > 0, AUC computable, tactical channel
moves) still hold, but the reliability table only populates ≤ 0.55.

v2 levers, in order of leverage:
1. **Strong identity over the MCP transport** — talk to the server via the MCP
   streamable-HTTP/stdio transport with caller-proven process binding instead
   of the REST token wrapper. Lifts the cap and unlocks the high bins. This is
   the real fix and the main v2 work item.
2. **Measure what the server actually scores** — accept that tactical
   calibration scores the EISV-corrected/capped confidence and frame the report
   as "is the governance system's *own* confidence well-calibrated," which is
   arguably the more useful question. report.py already bins on the registered
   value, so this needs only framing.
3. **Pre-distort stated confidence** to hit target *registered* bins by
   inverting the correction — brittle and does not defeat the 0.55 cap, so only
   useful in combination with (1).

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
