# CI coverage validation after #2101

The sys.monitoring coverage core and eight revised shards remain enabled.
The measured speedup is useful evidence for retaining the optimization.
Full-suite tracer equivalence has not been established by the published CI
comparison. This note corrects the original #2101 interpretation.

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
`src/mcp_handlers/lifecycle/resume.py`. The 36-statement denominator change
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
all eight shard commands from `.github/workflows/tests.yml` under `ctrace`
and `sysmon`, with separate `COVERAGE_FILE` paths for every core/shard pair.
Keep output directories separate when combining each core's eight files.

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
