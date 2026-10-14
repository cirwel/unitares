# Agent read-path check: appended notes

Classification: fix and evaluation. This is a navigation correction on existing
KG reads, with no new action, ranking change, model call, database migration or
automatic rewrite of an authored claim.

## Reproduced failure

A retrieved summary can still carry an old instruction while its appended notes
record that the work shipped. On the live README finding
`2026-06-19T13:42:12.087258+00:00`, the original summary retains a public-edit hold.
Its full details clear that hold in a later note. The default first 2,000-character
details page does not reach it: the final block starts at character 4,852 of 5,249.
Status `resolved` and `closure_class=fix_verified` are already visible, but neither
explains whether the original claim was confirmed or corrected.

The response now carries `resolution_notes` with its text source, block count,
first offset and last offset. A reader can jump to the last block or inspect the
history using the existing details call. There is no extracted claim or shortened
correction that could drop a qualifier. The locations are recognized headers in
authored text, not authenticated receipts. Last in text order does not mean true
or authoritative; a later administrative note can follow a substantive correction.

## Frozen navigation replay

The [aggregate result](results_2026-10-14.json) reuses the private snapshot from
the [serving ablation](../serving_ablation/README.md), identified by its SHA-256.
It serializes the real stored records through the handler and friendly envelope;
it performs no database writes, query encoding or external inference.

| Check | Result |
|---|---:|
| Default-visible records | 702 |
| Records with recognizable notes | 153 |
| Last note starts past the first details page | 75 |
| Last note starts beyond the embedding text window | 14 |
| Verified first/last offsets | 153/153 |
| Same note locations in lean, compact and full digest modes | 153/153 |
| Single-result lean response size | 1,775–2,078 bytes |

Across those 153 records, sequential default pagination takes 312 pages to reach
the last note headers (maximum 23 for one record); direct offsets take one read
per record. This is a mechanical comparison, not measured agent savings. An agent
could already request a larger page or infer a tail offset from total length.
One page reaches a header; long notes still need continuation. The full-mode
comparison here retains summaries rather than expanding all details inline.

The tests exercise Unicode offsets through the actual details handler, multiple
notes, an administrative last note, invalid headers, open and resolved records,
imported-content truncation, all search response modes and extra-row budgets.
Seven regression cases failed before the fix; all twelve pass after it. A text
replacement can invalidate an old offset: details reads return fresh locations,
which callers should compare before reusing a pointer.

Reproduce with access to the private snapshot:

```sh
python3 scripts/eval/kg_note_navigation.py /path/to/serving-snapshot.json \
  --output /path/to/navigation-results.json
python3 -m pytest -q tests/test_kg_resolution_note_reads.py
```

## Independent advice and remaining work

The operator requested Argon through AGY. A self-contained consultation selected
`gemini-4-argon-high` through the existing isolated subscription CLI. It completed
successfully; the CLI did not report a separate effective-model identifier. It
used no Google Search, repository tools or live KG access. Its answer is advisory,
not a PR review or an agent task benchmark. The private prompt/result are retained
under the session's `kg-agent-friction-20261014-493412bd` evaluation directory.

Argon prioritized correction visibility, current-policy versus historical-record
selection, and the blanket semantic-only confidence warning. We accepted the
navigation recommendation with both first and last offsets, without its proposed
excerpt, extra ranking diagnostics or multi-hop graph expansion. Some asserted
omissions were already implemented: lean rows can carry closure class and semantic
relevance, and explicit limits already expose rows beyond the first three. Advice
was checked against source rather than treated as a finding by itself.

Remaining evidence gaps are agent comprehension and decision quality, held-out
retrieval (especially current policy), branching/multi-hop correction use, and
no-answer behavior. The earlier 48-query pilot remains a selected development
fixture. Its default-digest top-three count is 23/48 before the earlier ranking
fix and 44/48 after it, computed from the preserved target ranks; top-five alone
would obscure that agent-facing boundary. This read-only presentation change
does not resolve its freeze-policy
ranking regression or make terms beyond character 6,000 visible to the embedding
model. Those are separate retrieval questions, and newest-first or recency is not
a substitute for explicit evidence about which policy applies.
