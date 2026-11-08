# KG memory workflow refinement

The private maintainer-planning replay exposed delivery and evaluation problems:
optional project tags excluded an existing material record, a query validation
error exhausted the bespoke harness's search budget, and short summaries hid
verified corrections. Those observations do not establish KG accuracy or general
utility. The earlier plans, receipts, invalid attempts and HOLDs remain unchanged.

The supported successor for offline workflow checks is
`scripts/eval/kg_context_trajectories.py`. It uses production search projection
and details pagination against synthetic in-memory records. It does not rank
queries or contact the live KG. Its existing source-range receipts distinguish
reading evidence from guessing an answer or citing a displayed title.

## Check scope before enrollment

```sh
python3 scripts/eval/kg_context_trajectories.py preflight \
  --output /path/to/new-scope-receipt.json
```

An optional `--tasks /path/to/fixtures.json` checks another document in the same
fixture format without enrolling it in a model run. `freeze` and `run` continue
to use the checked-in fixtures. No model or storage call is made by `preflight`.

Each task's `records` is its supplied catalog. Optional `scope` fields are:

```json
{
  "membership_source": "the explicitly provided capture manifest",
  "capture_ids": ["record-a", "record-b"],
  "required_source_ids": ["record-a"]
}
```

The expected evidence source is also a required control. The preflight reports
catalog/capture counts, unresolved IDs and missing controls, with content,
selection and control fingerprints. It never fills an omission, runs retrieval,
or infers project membership from tags. A passing receipt establishes consistency
only within the provided catalog, not a complete project inventory or truthful
claims. Choose and freeze controls before retrieval; do not replace tasks based
on which queries work. Fix an underspecified capture boundary rather than
retagging a known answer to rescue an old enrollment.

`freeze` includes this receipt. `run` recomputes it before even its transport
canary; missing or changed controls block execution. Old plans without it must
not be silently upgraded or used to recompute old scored results.

## Exercise recovery and source reading

The added synthetic cases cover an exact tag-filter miss followed by a separately
requested broader read, query validation followed by explicit reformulation, and
a checked correction followed by a newer unsupported proposal. Tag filters match
any supplied tag, as production does; they are never silently dropped.

The 100-byte limit appears only in its regression fixture. Its error reports the
actual UTF-8 byte count and limit, retains the request, and consumes one bounded
turn. It does not erase later turns or truncate the query. Other fixtures have no
such artificial cap. Repeated invalid requests still terminate at the episode's
turn limit. Search summaries, note offsets and replacement links use the existing
production serializer rather than an extra 140-byte projection.

The latest note is a location, not a truth verdict. A successful synthetic answer
must agree with the fixture's checked evidence, cite an actually inspected record
and have received the evidence's exact source range. This is an instrument check,
not proof of better model behavior, production retrieval, or completed software
work. No new metered benchmark is part of this refinement.

## Keep current claims visible

The canonical KG skill now distinguishes optional tags from project inventories,
requires evidence/scope checks before policy use, and explains why a verified
conclusion change needs a summary update as well as preserved history. Appended
notes alone leave the previous headline in search. Timestamps and lifecycle
status cannot settle conflicting evidence.

Live headline curation uses the existing update operation, separately from this
code change: retain private before/after content fingerprints, change only the
evidence-backed summary, and verify that details, status, tags and original
attribution remain intact. A verified implementation receipt is distinct from a
deployment receipt. Such curation changes future data; it does not improve or
revalidate the frozen planning result. The skill mirror follows the existing
canonical sync workflow; it is not edited by hand.
