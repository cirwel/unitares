# KG context trajectories

Classification: tooling/fix validation. This evaluation extends the response
projection replay in PR #2797 to sequential agent decisions. It introduces no
production tool, action, model channel, storage change or admission threshold.
It is separate from the gated KG adoption pilot; that pilot's enrollment and
execution controls remain unchanged.

## Question and conditions

Does removing repeated lean-search coaching reduce context use over a complete
bounded reading task while preserving the agent's ability to check evidence?

The conditions differ only in the search envelope builder: the pre-change
formatter at `7b717630e` and the current formatter. Both use the same production
search serialization, supersession annotation and unwrapped details reader.
Records and graph lookups are in memory, and read-event broadcasting is mocked.
There are no live KG reads or writes. Fixture metadata time is fixed at
`2026-10-14T08:00:00Z` so future replays do not silently change age markers.

Four authored synthetic tasks cover:

- Following a superseded policy to its replacement and checking the new value.
- Reading a correction beyond the first details page when the latest appended
  note is only administrative.
- Distinguishing an imported recollection from a checked configuration receipt.
- Reading an update delivered mid-task, then acknowledging its exact revision.

Search returns the task's fixed ordered result set for every query. This does
not evaluate embeddings, relevance ranking, corpus coverage or query quality.
Details pagination executes the production reader. The change-notice queue is
simulated: the first evidence-text read of the changing record delivers the old body and a
notice; later reads return the new body. An acknowledgement is accepted only
after that exact notice and its new details have been read. This is a controlled
task condition, not an integration test of the production notice store.

## Frozen execution and scoring

Before scored calls, `freeze` pins the task fixture, script, instructions,
relevant source trees, baseline formatter and runtime limits. Four tasks are
shuffled with seed 493412; the two conditions alternate order across tasks.
There is one episode per task/condition: eight planned episodes, no statistical
equivalence claim and no post-result acceptance threshold. Source changes make
the runner refuse the old plan. Results never replace a previous output folder.

Each episode gets a fresh isolated Argon CLI home/workspace and a new
conversation. Later turns resume that conversation; they do not start unrelated
single-question calls. The CLI uses its existing subscription login, plan mode
and sandbox flags. Instructions prohibit native CLI tools. The model's JSON
read requests are dispatched only to the fixture. Its available read interface
is deliberately smaller than the full advertised MCP catalog, so this is not a
native MCP client or whole production-harness comparison.

The fixed bounds are eight model turns, 60 seconds per call and 240 seconds per
episode. Provider failures end the episode. Invalid JSON or invalid fixture
requests use remaining turns and remain in the trace. There are no provider
retries, dropped attempts or selective task reruns. The runner first executes a
neutral two-turn transport canary and refuses scored calls if continuity or the
recall check fails. That receipt is retained separately and is not a task result.

Success requires the exact task answer, delivery of the required evidence text,
a citation to that record, and no citations to unread records. Character-range
coverage joins adjacent or overlapping pages within one source version; it
never joins across gaps or revisions. Complete evidence in a full-search preview
counts too, so the scorer does not force a redundant details call. Summary-only
digests do not count as delivery of their underlying evidence.
Answer correctness, evidence reading and citation validity are also reported
separately. Notice delivery counts, acknowledgements and premature attempts are
separate measures; an acknowledgement alone cannot make an answer correct.
The fixture and expected values are authored development cases, not a held-out
sample. They are never included in model prompts as answer keys.

Usage keeps the CLI's final cumulative session snapshot for each episode.
Resumed result snapshots must not be summed: they include earlier turns.
Input, output, thinking and cache counters remain separate, using the provider's
names. The largest input-counter delta is reported as peak **call** input;
context-window occupancy is unavailable. Missing or decreasing counters mark
usage incomplete, rather than inventing zero usage. Calls, errors, response
bytes and measured wall time per CLI invocation are also retained.

These counters include hidden CLI instructions and depend on caching and
provider behavior. They are not a tokenizer estimate, billable-cost calculation
or isolated measurement of KG text. See Google's [headless CLI accounting
documentation](https://www.antigravity.google/docs/cli/headless/) (checked
2026-10-14). The selected model is recorded; an effective-model receipt is not
inferred from the selector.

## Running

```sh
python scripts/eval/kg_context_trajectories.py freeze \
  --model gemini-4-argon-high --output /path/to/new-plan.json
python scripts/eval/kg_context_trajectories.py run \
  --plan /path/to/new-plan.json --output /path/to/new-results \
  --execute-argon
```

The second command explicitly invokes the subscription CLI; ordinary tests and
CI use scripted replies and never invoke a model. Keep full transcripts and CLI
receipts in the operator's private evaluation directory. Publish the frozen plan
and aggregate alongside this document. Per-episode receipts retain each prompt,
reply, fixture response, usage and failure. Each turn is checkpointed before its
model call and as its receipt/fixture result arrives, including an explicit
pending state for interrupted calls. Aggregate output is written after each
episode, so incomplete runs remain visibly incomplete. Unexpected fixture errors
are retained as harness errors and stop further scored work.

Offline regression controls exercise both formatters, replacement pointers,
long-note navigation, imported-memory markers, notice sequencing, unsupported
writes, unread citations, provider failures, turn exhaustion, usage accounting,
conversation isolation and changed-plan refusal.
The two historical-formatter integration cases require the baseline Git object;
they skip explicitly in shallow checkouts. Current-formatter controls still run,
and the live evaluation refuses to run without the historical source.

## Results: 2026-10-14

All eight scheduled episodes ran once under the [frozen plan](plan-20261014.json).
The neutral transport canary passed. The [corrected aggregate](aggregate-20261014.json)
contains every episode, including the unsuccessful one; no task was rerun.

| Task | Success before / after | Fixture calls before / after | Reported input before / after | Reported output before / after |
|---|---|---|---|---|
| Replacement | yes / yes | 3 / 2 | 39,538 / 20,332 | 1,152 / 895 |
| Imported claim | yes / yes | 2 / 2 | 20,676 / 20,513 | 990 / 822 |
| Appended correction | yes / **no** | 3 / 2 | 23,162 / 21,309 | 2,274 / 1,045 |
| Mid-task change | yes / yes | 4 / 4 | 27,526 / 25,891 | 1,301 / 1,058 |
| All attempts | **4/4 / 3/4** | **12 / 10** | **110,902 / 88,045** | **5,717 / 3,820** |

The failed lean-format correction episode returned a provider `ERROR` for an
improperly formatted function call on turn three, after reading the initial
details page. It did not reach the correction or produce a final answer.
Its usage receipt is present and included. This observed provider failure does
not identify the format as its cause, but it remains a failed task in that arm.

All seven completed answers had the expected value, delivered supporting
evidence and valid citations. Both change-notice episodes read the updated
configuration, acknowledged its exact revision, and had no premature
acknowledgement attempts. In the replacement pair, the old-format agent read
the obsolete record before its successor; the lean-format agent went directly
to the successor. The imported-claim pair took the same two reads in both arms.

Across all attempts, tool responses were 16,104 versus 11,267 UTF-8 JSON bytes,
provider calls were 16 versus 14, and reported cache reads were 157,843 versus
156,554 tokens (before versus after). These totals include the failed episode's
early termination. They do not establish lower cost for equally successful
work. One authored pair per task, provider/cache variability, and the 4/4 versus
3/4 outcome do not establish equal reliability or a general context-savings
percentage. The earlier frozen-response replay in #2797 remains the evidence
for the narrower 23.49% response-byte reduction.

### Accounting correction and audit trail

The execution at `bdd7dda0adcf5062aeeeb6f2ee9f3b6705b113e9` used the original
reviewed v1 reporter. During the run, cumulative `num_turns` and cache counters
prompted a check of the provider documentation. That exposed an error in the
reporter: it summed cumulative session snapshots and mislabeled their maximum
as peak input. Those original usage totals are invalid and are not the table
above. The frozen plan, original aggregate and all raw receipts remain intact.

The v2 reporter corrects accounting offline from those same receipts. Task
prompts, conditions, schedule, evidence scoring and outcomes did not change.
The published aggregate pins the original aggregate and every source receipt by
SHA-256, as well as the correction script. All eight corrected usage streams
are complete and monotonic; context-window peak remains unknown. Full receipts
stay in the operator's private evaluation directory.

To reproduce the correction without another model call:

```sh
python scripts/eval/kg_context_trajectories.py report \
  --run-dir /path/to/original-results --output /path/to/new-aggregate.json
```

The correction also applies to future runs. Regression controls check
cumulative snapshots, separate cache counters, missing/decreasing usage, and
preservation of task scores and original receipts during offline reporting.
