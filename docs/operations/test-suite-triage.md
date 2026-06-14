# Test Suite Triage

Last triaged: 2026-06-14.

Default full gate:

```bash
./scripts/dev/test-cache.sh
```

As of this triage, the default suite has 21 expected skips. The important
distinction is that default skips should be opt-in integration drills, not stale
unit coverage hidden behind unconditional `pytest.mark.skip`.

## Expected Default Skips

| Area | Count | Default reason | Opt-in |
| --- | ---: | --- | --- |
| Governance failure-mode drills | 13 | Requires running governance/Pi MCP endpoints | `RUN_GOVERNANCE_DRILLS=1 pytest tests/drills/test_failure_modes.py -v` |
| OpenAI-compatible HTTP endpoints | 5 | Requires running REST server and explicit endpoint opt-in | `RUN_OPENAI_ENDPOINT_TESTS=1 pytest tests/test_openai_endpoints.py -v` |
| Legacy dialectic protocol drill | 2 | Requires registered live test agents/session state | `RUN_DIALECTIC_PROTOCOL_TESTS=1 pytest tests/test_dialectic_protocol.py -v` |
| Wave 3a live BEAM round-trip | 1 | Operator-led BEAM/MCP drill | `WAVE_3A_RUN_LIVE_ROUNDTRIP=true pytest tests/integration/test_wave_3a_response_parity.py -k test_health_check_live -v` |

## Closed Gaps

- CI and local full gates now both run `tests/ agents/`.
- CI, `make test`, and `test-cache` now share a 75% coverage floor.
- `tests/test_doc_drift.py` now reads canonical in-repo `skills/`, so code-doc
  drift checks run in ordinary worktrees instead of skipping when the companion
  plugin checkout is absent.
- `tests/test_dialectic_discovery.py` no longer hides stale DB APIs behind a
  class-level skip; it covers the current pending-dialectic enrichment path.

## Follow-Up Candidates

- Split live drills into a dedicated CI job with service setup instead of
  keeping them default-skipped forever.
- Retire or rewrite `tests/test_dialectic_protocol.py`; deterministic dialectic
  behavior is already covered by handler and pure protocol tests, while this
  file still depends on live registration state.
- Add a periodic scheduled job for the OpenAI-compatible HTTP endpoint tests
  once the server boot path is cheap and deterministic enough for CI.
