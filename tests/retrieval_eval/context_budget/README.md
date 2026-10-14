# KG lean context budget

Classification: fix. This removes repetitive coaching from routine search reads
while preserving the routes to evidence and richer response modes. It adds no
tool, action, model call, storage mutation or ranking rule.

## The repeated cost

A lean search response used to explain how to store and update findings in its
next action, repeat the details route in a retrieval-options object, and repeat
mode selection in both a tier ladder and the full-response hint. These strings
were emitted even for a one-record read. The 3 KB digest budget limited maximum
size, but small responses still carried the tutorials on every call.

The lean response now gives a short read-oriented next action. It keeps the
result tier and one details route, plus the disclosure when a requested inline
details expansion was overridden. The full-response route stays available.
Compact mode keeps serialization diagnostics and mode coaching; full mode keeps
the canonical fields. Confidence and degradation warnings are retained, along
with the existing rules for attribution, imported memory, supersession and note
locations. Original summaries are not rewritten or model-compressed.

Removing optional prose exposed a boundary assumption in attribution restoration:
the old formatter relied on coaching being available to make room for an omission
marker. At the exact byte boundary, lean could otherwise withhold attribution
without that marker. The final budget fallback can now shorten the full-mode hint
to its executable call, freeing enough space while keeping navigation intact.
The existing sweeps check that attribution costs no result or result field, an
identifier is either whole or omitted, and withholding remains explicit.

## Frozen replay

The [aggregate result](results_2026-10-14.json) uses the same private 702-visible-row
snapshot as the embedding and note-navigation experiments. The SHA-256 pins its
contents. The evaluation creates one-record reads, contiguous groups of three,
groups of ten with default and explicit limits, and an empty result. These are
1,079 projection cases, not independent queries or retrieval-quality trials.

| Measurement | Before | After |
|---|---:|---:|
| Lean response bytes, summed across cases | 2,331,826 | 1,783,984 |
| Median lean response bytes | 1,787 | 1,249 |
| 95th percentile response bytes | 4,199 | 3,660 |
| Visible result occurrences | 2,309 | 2,309 |
| Visible replacement-pointer occurrences | 56 | 56 |

Total serialized lean bytes fall **23.49%**. The comparison verifies zero lost
previously visible rows, zero changed checked evidence fields, and identical
compact and full envelopes in all 1,079 cases. A record appears in multiple cases;
the occurrence count is not a count of unique records. The larger responses
include explicitly requested extra rows, which have a separate 2 KB budget.
Full mode here omits inline details.

The preserved-field check covers author identity and label, lifecycle and closure
markers, imported-memory provenance, supersession, note offsets, relevance and its
basis, detail availability, tags and summary content. A formerly shortened summary
may regain a longer preview; no earlier preview content may disappear. These
checks concern the visible projection, not the truth or completeness of a record.

Review caught that the initial replay omitted production supersession annotation,
making its replacement-pointer comparison vacuous. The corrected replay calls
the production annotator with a separately frozen AGE successor map: 22 of the
41 superseded corpus rows have replacement edges. This sidecar was captured
read-only at `2026-10-14T07:17:09.452934+00:00`, after the original corpus snapshot,
so it is not a reconstruction of the graph at the original capture time. Both
conditions use the same map; its hash and timestamp are in the aggregate. The
comparator now requires nonzero visible replacement-pointer coverage. Three
regression tests exercise annotation, pointer loss and the empty-coverage trap.

## Reproduction and limits

A supplemental [blinded reading probe](reading_probe_2026-10-14.json) asked the
selected `gemini-4-argon-high` model nine concrete questions in both formats,
shuffled in one text-only call. Condition labels and expected answers were hidden.
It answered **9/9 before and 9/9 after**: replacement IDs, note offsets and their
unverified status, imported-memory trust, omitted inline details, semantic-only
matching, extra-result IDs, withheld authorship and an empty result set. The
subscription CLI completed successfully but supplied no separate effective-model
receipt. The prompt, answer key and unedited response are retained privately with
the captures. This is a synthetic reading check in one shared context, not an
independent agent trajectory or evidence of statistical equivalence.

Use `scripts/eval/kg_context_budget.py capture SNAPSHOT --successors SUCCESSORS.json
--output CAPTURE.json`
against the pre-change and final code, then run `compare BEFORE.json AFTER.json
--output AGGREGATE.json`. Captures contain private corpus text; publish only the
aggregate. The script reads no live database and invokes no model. The baseline
is master commit `7b717630e`; copy the same evaluation script into that checkout
before capturing its responses. The successor sidecar has `captured_at` and a
`successors` object mapping old discovery IDs to lists of replacement IDs from
AGE's directed `SUPERSEDES` edges. Private captures (`before-with-successors.json`,
`after-with-successors.json`) and `successors.json` are retained in the operator's
`kg-context-budget-20261014-493412bd` evaluation directory. Earlier captures remain
there for audit; their 23.57% reduction is superseded by the corrected replay.

Six targeted tests check default and explicit lean mode, expansion routes,
requested-detail omission disclosures, caveat retention and diagnostic modes;
four failed before the fix. Broader alias tests exercise the mounted MCP path.
Response-budget and attribution sweeps cover large labels, legacy identifiers
and confidence notes near the 3 KB boundary.

The metric is UTF-8 JSON bytes, not tokenizer counts or measured agent savings.
The replay excludes later change-notice middleware, accumulated conversation
history, tool-schema advertisement and initial instruction loading. It does not
measure agent decision accuracy or live latency. The small reading probe does not
replace task traces with the same evidence and independent outcome scoring.

Agents should use the existing expansion paths deliberately: search, open only
relevant records, retain scope/caveats and evidence IDs in the working summary,
and acknowledge exact change-notice revisions after processing them. Acknowledging
unread changes or muting notices to shrink context would discard useful evidence.
