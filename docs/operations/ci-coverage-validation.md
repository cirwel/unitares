# CI coverage validation after #2101

The sys.monitoring coverage core remains enabled. The eight revised shards
were replaced on 2026-09-17 by four shards on a single Python 3.14 leg.
The measured speedup is useful evidence for retaining the optimization.
Full-suite tracer equivalence has not been established by the published CI
comparison. This note corrects the original #2101 interpretation.

## Python 3.15 transition

The full suite runs the same four file partitions on Python 3.14 and 3.15.
3.14 remains the server support floor and the only source of combined coverage;
3.15 exercises the production runtime. The SDK's separate package job still
checks its 3.12 floor. Native bootstrap acceptance, installed server-wheel
imports, newest admitted MCP, and macOS platform tests also run on 3.15.

This adds four database-backed jobs per run. Retaining one selector avoids
creating a second test inventory or one long unsharded job. The workflow
cancels superseded revisions. Revisit the layout if measured queue pressure
warrants it; the 75% floor and complete file partition stay unchanged.

The aggregate gate compares exact interpreter/shard job names with the expanded
matrix. Missing, duplicate, unexpected, skipped, cancelled and failed legs
cannot count as complete evidence. API lag and failed reads retain a bounded
retry budget, including partial reruns across attempts.

The existing MCP transport and NumPy/SciPy tests provide runtime evidence;
`test_eisv_instrument_preservation.py` must pass its registered golden unchanged.
Docker Quickstart and Glama Bundle build the two promoted images; native
acceptance exercises fresh installation and interrupted migration recovery
against disposable PostgreSQL and Redis services. Release promotion selects
the interpreter from the exact release tag's production marker, so an older
release is not silently tested with the current development default. Ordinary sharded CI still
does not close the opt-in AGE singleton gap documented below.

For local compatibility work, use an isolated 3.15 environment and select tests
that do not request `live_postgres_backend`. The local `governance_test` database
is shared across worktrees and its fixture truncates tables. Run complete write
paths in CI's disposable services. Optional embeddings need a separate CPU-only
install/import check; Quickstart's Compose-config check does not exercise them.

Promoting repository images does not replace a running native environment.
Native deployment uses its owning deployment command, with a fresh 3.15 venv;
retain the prior 3.14 environment and image for rollback until the new process
passes readiness. Do not copy a venv across minor versions or edit a managed
deployment checkout.

## Four-shard rebalance, 2026-10-20

The alphabetical boundaries are now `a-d`, `e-k`, `l-r`, and `s-z` with
nested tests and `agents/` in the last shard. All tests, four isolated
database-backed jobs, sysmon coverage, and the combined 75% floor remain.

Three completed runs motivated the change:

| Run | Old a-e / f-m / n-t / u-z+agents test steps (seconds) |
| --- | --- |
| [39395963945](https://github.com/cirwel/unitares/actions/runs/39395963945) | 411 / 337 / 314 / 145 |
| [39397715551](https://github.com/cirwel/unitares/actions/runs/39397715551) | 486 / 422 / 384 / 144 |
| [39399058287](https://github.com/cirwel/unitares/actions/runs/39399058287) | 475 / 422 / 451 / 146 |

For each shard log, subtract consecutive timestamped pytest file-progress
lines and sum those intervals by the file's first letter. Exclude the first
file in each job, whose preceding interval contains collection and startup.
Assign nested/agent file intervals to the last group. On the latest run,
these file-progress intervals total approximately 403 / 359 / 391 / 125
seconds under the old layout, and 303 / 342 / 328 / 305 under the new one.
The other two runs also favor these boundaries: their modeled maximum drops
from 355 to 282 seconds and from 414 to 340 seconds, respectively.

These are scheduling estimates, not measured executions of the new shards:
log buffering, collection, coverage finalization, runner differences and
changed test order can affect the result. Compare the subsequent CI test
steps before claiming an end-to-end saving. Re-cut from fresh timings when
the distribution drifts; adding runners is not necessary for this rebalance.

`tests/test_ci_test_gate.py` executes the workflow's Bash target selector and
checks that every test file belongs to exactly one shard. Deliberately
missing and overlapping selections exercise the guard. This checks the
actual selector rather than a separate Python copy of its letter ranges.

## What the CI runs establish

| Run | Actual checkout | Combined statements / missing | Coverage |
| --- | --- | --- | --- |
| [34172408026](https://github.com/cirwel/unitares/actions/runs/34172408026) | `75c49c4a6c6db190f27f9aa37997aee9cef8d408` | 58,936 / 9,498 | 83.88% |
| [34176589638](https://github.com/cirwel/unitares/actions/runs/34176589638) | `9c4da1bcdf74c1efe0e5bc425d4cd8fab6648d78` | 58,900 / 9,499 | 83.87% |
| [34178043046](https://github.com/cirwel/unitares/actions/runs/34178043046) | final PR head `4eed7189b785310570a08e892dbcd6dcb6160aa4`, tested through the PR merge ref | 58,985 / 9,506 | 83.88% |

The second run's head was `898d797`, but Actions checked out the synthetic
merge `9c4da1b` into master `0697f14`. The
[actual tree comparison](https://github.com/cirwel/unitares/compare/75c49c4a6c6db190f27f9aa37997aee9cef8d408...9c4da1bcdf74c1efe0e5bc425d4cd8fab6648d78)
includes intervening source edits, including removal of
[the former lifecycle resume handler](https://github.com/cirwel/unitares/blob/75c49c4a6c6db190f27f9aa37997aee9cef8d408/src/mcp_handlers/lifecycle/resume.py). The 36-statement denominator change
cannot be attributed just to imports or sharding.

The first two runs recorded 228 -> 144 seconds for the slowest Python 3.12
test step and 1,128 -> 842 seconds for the sum of those steps. These are
observed job-step timings, not an isolated estimate of the tracer's causal
effect. The Python 3.12 / uninstrumented Python 3.14 ratio also changes the
interpreter. Overall elapsed workflow time includes runner queueing.

Similar aggregate percentages support the existing combined 75% floor, but
can conceal offsetting gained and lost lines. Trading a covered success path
for a timeout path does not rule out another measurement loss. Report the
actual gained/lost line sets before attributing differences to timing.

## A controlled core comparison

Use one unchanged checkout, interpreter, installed dependency set, database
state and shard layout. Record the checkout SHA, Python and coverage.py
versions, selected core, collected/passed/skipped counts and warnings. Execute
every shard command in `.github/workflows/tests.yml` under `ctrace`
and `sysmon`, with separate `COVERAGE_FILE` paths for every core/shard pair.
Keep output directories separate when combining each core's per-shard files.

Generate `coverage json` reports after each combine. Compare file inventories,
statement sets and per-file `executed_lines`, including both directions of
the set difference; the total percentage alone is insufficient. Investigate
differences using deterministic success/timeout fixtures, and disclose any
remaining unexplained differences. A repeated subset result is evidence only
for that subset.

`test-cache.sh` hashes the normalized core selection and coverage.py version.
Unset, empty and explicit `sysmon` select the same coverage run; changing to
`ctrace` invalidates its cache. Use `--fresh` for an intentional repeat even
when the selected core and source inputs have not changed.

## Opt-in drills outside the default gate

Some suites skip in the default gate (`./scripts/dev/test-cache.sh` locally,
the sharded workflow in CI) because they need live services or registered
state. A default skip should be one of these opt-in drills, not stale unit
coverage hidden behind an unconditional `pytest.mark.skip`. Each runs only
when its environment flag is set:

| Area | Default reason | Opt-in |
| --- | --- | --- |
| Governance failure-mode drills | Requires running governance/Pi MCP endpoints | `RUN_GOVERNANCE_DRILLS=1 pytest tests/drills/test_failure_modes.py -v` |
| OpenAI-compatible HTTP endpoints | Requires running REST server and explicit endpoint opt-in | `RUN_OPENAI_ENDPOINT_TESTS=1 pytest tests/test_openai_endpoints.py -v` |
| Legacy dialectic protocol drill | Requires registered live test agents/session state | `RUN_DIALECTIC_PROTOCOL_TESTS=1 pytest tests/test_dialectic_protocol.py -v` |
| Wave 3a live BEAM round-trip | Operator-led BEAM/MCP drill | `WAVE_3A_RUN_LIVE_ROUNDTRIP=true pytest tests/integration/test_wave_3a_response_parity.py -k test_health_check_live -v` |
| Live AGE knowledge-graph singleton | Requires an AGE-enabled `governance_test` database | `UNITARES_TEST_AGE_SINGLETON=1`, described in the next section |

Skip counts move with the suite, so none is recorded here; read the current
count from a test run. The dated 2026-06-14 triage that first listed these
drills, with its follow-up candidates, is archived at
[`archive/test-suite-triage.md`](archive/test-suite-triage.md). A green
ordinary run says nothing about any drill in this table.

## Knowledge graph lifetime and the live AGE gap

`tests/test_knowledge_graph_singleton.py` checks that the factory starts each
test with no cached graph or lock and reuses one loaded graph within a test.
Its ordinary cases mock backend loading; they do not establish AGE behavior.

On an existing AGE-enabled `governance_test` database, run:

```bash
UNITARES_TEST_AGE_SINGLETON=1 python -m pytest \
  tests/test_knowledge_graph_singleton.py -k live_age -q -ra
```

Both live cases must pass without skips. They use the real singleton factory,
fresh database backends and pytest event loops, exercise warm reuse and the
next test's reconstruction, then query AGE and the knowledge store. They use
the standard test-schema bootstrap and existing test database; graph loading
may initialize indexes or rehydrate AGE. They do not truncate tables, create
another database, or use the production connection singleton.

Without explicit opt-in these two cases skip in ordinary CI. With opt-in,
missing PostgreSQL or AGE fails the run. A green ordinary CI run therefore
does not close the live AGE gap. Record an actual non-skipping run before
claiming that gap is closed.
