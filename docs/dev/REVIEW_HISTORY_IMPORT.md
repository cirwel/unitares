# Review-history importer

**Status: proposed tooling, pending operator adoption (2026-11-01).**

`scripts/dev/ingest_review_records.py` recovers the uncommitted review-ledger
prototype based at `a6756663e`. It reads review-gate issue comments on closed
pull requests and proposes one searchable historical observation per PR.
It runs manually and defaults to a dry run. There is no scheduler or server
registration.

## Existing capability and proposed addition

The [intent map](../integration/INTENT_MAP.md) already routes durable evidence
to `knowledge`, and existing `ingest_ci_findings.py` provides GitHub-to-graph
ingestion. This importer uses those existing transport and knowledge actions.
It adds an optional CLI producer and a `review-history-v1` observation format;
it does not add an MCP tool, action, model channel, or calibration input.
Adoption of that producer and format is the operator decision reserved by
AGENTS.md's new-tools/instruments rule. Merging the producer does not schedule
it or run a backfill.

## Preview

Run from the target checkout:

```sh
python3 scripts/dev/ingest_review_records.py \
  --repo OWNER/REPO --pr NUMBER --dry-run
```

The preview reads GitHub and searches the graph for duplicates. It does not
store or resolve discoveries. An unavailable graph or an unreadable response
ends the preview rather than reporting that no duplicate exists.
GitHub reads use the installed `unitares-github --read-only` runner, scoped
to `--repo`. Without that runner, a provisioned `GH_TOKEN` or `GITHUB_TOKEN`
is required; the CLI does not fall back to an ambient human login.

After adoption, `--apply --client-session-id SESSION` enables writes. SESSION
must be the importing process's binding from `start_session(force_new=true)`;
a bearer token alone does not supply attribution. The importer does not
onboard or infer an original author's identity. Existing REST endpoint and
bearer environment variables have the same meaning as in `ingest_ci_findings.py`.

## Observation format and evidence limits

Each observation contains a repository/PR marker, a SHA-256 source fingerprint,
the import timestamp, original comment timestamps and URLs, every accepted
review round, and bounded excerpts of findings and dispositions. Tags include
`historical-import` and `review-history-v1`. The importing session is the writer.
Source model identifiers remain self-reported source metadata.

Comment eligibility uses `review_gate.trusted_review_comment`, including its
legacy contributor associations and API-identified review Apps. The observation
records the policy and trust metadata used **at import time**. It does not
reconstruct what the trust policy was when the original comment was posted.
Only review-gate issue comments are covered; native inline reviews are omitted.

Disposition categories are text-reading aids: accepted, rebutted, unclassified,
superseded or undisposed. “Superseded” only means a later completed round
reviewed another diff; it does not prove that an earlier finding was fixed.
Dispositions remain self-attested claims. No `outcome_event` is called and no
verified outcome or model-quality score is created.

Round metadata must fit within 48,000 characters. Review bodies and disposition
text may be shortened, with source URLs retained. Excess metadata and invalid
or excessive finding counts fail before a store. Full source text is not
promised as a graph mirror.

## Repeated and interrupted runs

The source fingerprint covers the PR metadata and accepted review/disposition
comments, including their content and edit timestamps. A matching historical
observation is skipped. Edited or late comments produce a reconciliation error;
the importer never silently overwrites an earlier snapshot. Closed PRs remain
mutable, so an import is not a claim that their history is final.

Storage is followed by a resolve update and status readback. If interrupted
after storage, an identical rerun repairs an open observation rather than
storing it again. Dry runs report that repair without writing it. Conflicting
snapshots, multiple matching rows, and incomplete search evidence require
explicit reconciliation. Run one importer per repository at a time: the
search/store sequence is not an atomic uniqueness constraint.

The first live backfill should be a small, separately authorized batch with
stored rows inspected afterward. This recovery has not run `--apply` against
a live graph.

## Related recovery disposition

The separate dormant `claude/queue-veto-deps` commit `8c04f4b92` is preserved,
not restored by this recovery. Its MERGED-only dependency rule differs from
the current [queue contract](../operations/github-workflow-conventions.md),
which also accepts CLOSED. This allows a dependent PR to proceed after an
unmerged predecessor is closed; whether to replace that policy remains a
separate decision. Its `automerge-hold` veto also exceeds the current documented
role of muting the disarm detector: the existing label does not guarantee that
an already armed PR cannot merge. Neither policy is silently changed here.
