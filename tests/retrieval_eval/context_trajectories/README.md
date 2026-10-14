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

Usage sums the CLI's reported counters over every turn, retaining input, output,
thinking and cache counters separately. Thinking/cache counters are not added
again to input/output totals. Peak reported input tokens, calls, errors, response
bytes and elapsed model-call time are reported too. Missing usage stays missing,
with `usage_complete=false`; it is not replaced by zero. These counters include
hidden CLI instructions and may vary with caching and provider behavior. They
are not a tokenizer estimate, billable-cost calculation or isolated measurement
of KG text. The selected model is recorded; an effective-model receipt is not
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

## Results

Pending the first frozen run. No task or token-savings claim is made by this
protocol alone.
