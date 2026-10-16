# Cross-agent knowledge archival: operator decision

**Status:** Operator decision recorded 2026-10-13: retain collaborative low/medium
curation. Kenny approved the recommendation with “proceed” in the KG-audit
follow-through. This records policy; it changes no runtime permissions.
**Evidence checked:** 2026-10-13.

## Observed facts and boundary

The KG usability audit (discovery `2026-10-13T06:48:37.234783+00:00`) reported
that its bound writer could archive another writer's row. The code supports
that observation for **low/medium severity**. It does not support the broader
claim that every bound agent can archive every row.

`src/mcp_handlers/knowledge/handlers.py` implements the distinction in
`_resolve_update_writer`, `_effective_update_severity`,
`_authorize_high_severity_update` and `_requested_non_owner_edits`:

- Low/medium updates do not enter the per-discovery ownership gate. Bound callers
  can change another author's status, including archival. The low-friction
  writer path can also attribute eligible unproven writes anonymously; binding
  and ownership of a particular discovery are distinct checks.
- Effective high/critical severity (the higher of stored and requested severity)
  requires an owned caller binding. The original author may edit the row.
  A non-owner is limited to `resolved`, `closed` or `wont_fix`, with closure
  notes/evidence in the same call. A non-owner cannot use `archived` there or
  rewrite the summary/details/tags/severity. This severity gate prevents a
  downgrade from bypassing it.
- Closure classes and evidence make a disposition auditable. They are not
  proof that the writer owns the discovery or that its conclusion is true.

The follow-up inspected code and existing tests; it did not perform another
cross-agent authorization probe or alter any guard. Its native-row triage was
explicitly delegated by the operator. Permission for that triage is not a
general policy change. Security findings remain subject to the operator's
reserved dismissal authority regardless of what the API accepts.

## Options

| Option | Effect | Tradeoff |
|---|---|---|
| Keep collaborative low/medium curation | Preserve current cross-agent archive rights; improve documentation and audit visibility | Easy shared maintenance, with broad mutation authority |
| Owner or operator/delegated curator | Require an explicit authority for another author's archival; preserve a separate evidence-based closure policy | Clearer control, but needs a way to curate rows from exited authors |
| Proposal then authorized disposition | Let another agent propose closure; owner/operator/curator makes the status change | Strong review boundary, extra queue and orphan-handling work |

## Adopted decision — 2026-10-13

Retain collaborative archival of low/medium findings. Each disposition needs
an attributed reason and evidence, with a closure class when applicable. Age
alone is never sufficient. Search and inspect the source before closing a row.
Imported memory remains owned by its sync writer and is not manually archived.

Preserve the existing high/critical checks and cross-agent terminal-closure
exception. Security dismissals remain operator-reserved. This does not authorize
content replacement or severity downgrades as a substitute for archival review.

The decision avoids making exited authors the only people able to maintain
their findings. The rejected alternatives above remain as design history.
No authorization code or production data was changed to record this decision.
The approval and delivery evidence are appended to original audit
`2026-10-13T06:48:37.234783+00:00`.
