# Proposals and decision history

Start with the [product definition](../PRODUCT_DEFINITION.md),
[architecture](../UNIFIED_ARCHITECTURE.md), [interface](../INTERFACE_CONTRACT.md),
and [evidence and limits](../EVIDENCE_AND_LIMITS.md) for the current system.
This directory holds the research and engineering record behind it.

## Choose a reading path

| You need | Start here |
|---|---|
| Current designs, open decisions, and operative contracts | [Current proposals and contracts](active/README.md) |
| Frozen experiments, registered protocols, and stop rules | [Registered protocols](registered/README.md) |
| Completed work, parked ideas, negative results, and historical decisions | [Proposal archive](archive/README.md) |

Each document's body states its own status and is authoritative. Folder placement
expresses a reading purpose; it changes no decision, authorization, registration,
or scientific result. Current contracts stay in `active/` even when an
implementation is Built. The archive includes Parked questions that may resume.

## Dispositions

The child indexes preserve the disposition reading taken on **2026-09-03**,
plus subsequently added rows. A proposals audit on **2026-09-23** re-read the
rows whose status the code, git history or the maintainer deployment had
overturned (retagging 13 of them and annotating others), and, at the operator's
selection that day, parked five Active rows whose documents are untouched for
30 days and wait on nothing in flight; rows it
did not flag keep the 2026-09-03 reading. The tags are not a current work queue or permission
to build. In particular, an Active row can be blocked or already partly built.
Read its body and linked implementation before choosing work.

| Tag | Meaning in the index |
|---|---|
| **Built** | The status says shipped, implemented, landed, or wired, in whole or part. Dormant means built but flag-off or unwired; partial means a named phase shipped. |
| **Registered** | A frozen or pre-registered protocol. Its stop rule binds the analyst and is never re-run, refreshed, or weakened. |
| **Active** | Design or measurement work touched in the 30 days before the reading date (2026-09-03, or 2026-09-23 for rows re-read then), or named by a signed gate as in progress. This was a chosen sorting rule. |
| **Parked** | Design-only or deferred by its status and untouched for 30 days at the reading date (before 2026-08-04 at the 2026-09-03 read; before 2026-08-24 at the 2026-09-23 re-read), with nothing in flight waiting on it. The row retains its recorded date. |
| **Closed** | A recorded decision, refutation, superseded draft, negative result, or dated record retained as provenance. |

Current counts:
Built 26 · Registered 9 · Active 19 · Parked 26 · Closed 15

These counts cover the tagged entries across all three indexes. The archive
also preserves the 19 older records previously indexed under `resolved/`, and
one supporting JSON artifact. The counts are mechanically checked; the tags
remain a dated reading. Protocols that register at merge are tagged
Registered from their merge commit, even where the body keeps its DRAFT header.

`scripts/dev/check_proposals_index.py` checks recursive coverage, one row per
document, links, status fields and count arithmetic. It does not judge readiness,
re-tag work, or turn archival into a decision about value.

## Working on a proposal

The live lease-plane and BEAM contracts remain single-writer surfaces under
[`AGENTS.md`](../../AGENTS.md). Check open PRs and claim the surface before editing.
Preserve registered instruments and their authorization boundaries.

The [migration record](../dev/proposals-layout-2347.md) records the inventory,
placement exceptions, reference checks, and compatibility limits for #2347.
The existing Wave 1/Wave 3a locators, the published outcome-grounding stop rule
and accountable-testbed preregistration locators, and paths embedded in historical SQL migrations and frozen
evaluation evidence remain.
The legacy `resolved/` directory contains only a migration compatibility pointer.
The incremental-value and orientation protocols retain their original paths
and bytes because manifests or enrollment bind their digests; they are listed
in the current-work and registered indexes respectively.
GitHub does not redirect other
moved `blob/master` links; the inventory's source commit preserves every original
document. Frozen and historical prose changed only where reference paths moved.
