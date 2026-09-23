# Review-to-correction traces — 2026-09-21

## Status and question

This is a retrospective operational audit of recent UNITARES repository work.
It asks a narrow question: **does the development record contain review findings
that can be traced to concrete revisions?**

It does not test whether UNITARES outperforms GitHub review, a structured
handoff, self-review, or an unreviewed control. It does not estimate incident
prevention, net productivity, or the causal contribution of EISV signals.

## Sample

The sample was fixed before inspecting findings: the 20 most recently created
merged pull requests returned by GitHub at selection time on 2026-09-21, with
creation dates from 2026-09-18 00:20:02 UTC through 2026-09-21 02:51:36 UTC.
Documentation, dependency-bot, and apparently unreviewed changes remained in the
denominator. The selection is recent and non-random; it is not representative of
all UNITARES development.

For each PR, the audit read the available body, comments, and surviving commit
history. It inspected the corrective patches for attributed cases and checked
the recorded Tests workflow at the selected head. The audit did not rerun those
tests. Author-reported local counts remain author reports.

## Result

| Classification | PRs | Meaning |
|---|---:|---|
| Published finding followed by a matching revision | 8 | Strongest public trace available in this sample |
| Author-recorded internal review with a matching revision | 2 | Correction visible; reviewer provenance less independently established |
| Earlier structured-review condition implemented in a new PR | 1 | Cross-session continuity, counted separately |
| Pre-PR review or veto activity reported | 1 | Screening evidence, not a post-opening revision |
| Clean review with no attributed correction | 1 | Review occurred and correctly may have changed nothing |
| Review causality unestablished | 7 | Missing attribution is not evidence of no review or no value |
| **Total** | **20** | **10 within-PR review-to-revision traces; one additional carried condition** |

The 10/20 figure measures documented attribution in this selected sample. It is
not an estimate that review helps only half the time, and it does not support a
claim that nearly every review improves a PR. Commit counts were not used as a
proxy because merges, rebases, generated files, and CI repairs inflate them.

## Five inspectable traces

### Stored evidence became stronger than the acknowledged evidence — PR #2316

A reviewer found that an outcome capped at `tool_observed / 0.65` was graded
again in the database mixin and stored as `substrate_observed / 0.85`, while
the caller still received the lower grade. Four direct writers also bypassed
the recorder enumerated by the existing tests.

- [Finding and reproduction](https://github.com/cirwel/unitares/pull/2316#issuecomment-5743705868)
- [Author confirmation and disposition](https://github.com/cirwel/unitares/pull/2316#issuecomment-5743775071)
- [First corrective commit](https://github.com/cirwel/unitares/commit/17d8273b488601483344a67de29382de798c5c5d)

The revision moved the safe default into the grader, preserved already graded
detail at persistence, and retargeted coverage at the database write path.
Later commits added further hardening, so the linked commit is the first
correction in the chain rather than a claim that this one commit is a complete
security boundary.

### A decision-facing rate could exceed one — PR #2308

The PR's recorded review found that the analysis counted firings from all live
rows but divided them by first-person rows only. Two clean first-person rows and
six pronoun-free firings produced a rate of 3.0. The detector did not require a
first-person pronoun, so the exclusion's premise was false.

- [Pull request and review account](https://github.com/cirwel/unitares/pull/2308)
- [Corrective commit](https://github.com/cirwel/unitares/commit/6b4ad0eb1cf4d3816fbf74994c285d560f76f881)

The correction computes each numerator and denominator inside the same stratum,
retains pronoun-free firings, separates applied from shadow rows, and adds the
eight-row regression. This corrects the aggregation; it does not establish the
detector's real false-positive rate, which still requires semantic adjudication.

### A formatting repair could manufacture approval — PR #2321

A reviewer observed that the repair prompt merely instructed a model to preserve
its prior position. An unparseable prose rejection followed by parseable approval
JSON could therefore be filed as approval.

- [Finding](https://github.com/cirwel/unitares/pull/2321#issuecomment-5744472584)
- [Correction and regression tests](https://github.com/cirwel/unitares/commit/9c61df36678dd17589f6cb08ebcc8ab6d99af075)
- [Disposition of the remaining asymmetric-repair objection](https://github.com/cirwel/unitares/pull/2321#issuecomment-5744494027)

The revision refuses to file a repaired approval and adds tests for both the
prohibited reversal and an accepted repaired rejection. The trade-off is
explicit: it can discard a genuine approval whose first answer only had bad
formatting.

### A later clean result could erase an open finding — PR #2318

The first review-gate implementation selected the latest result for a diff. A
later `CLEAN` record could therefore replace an earlier unresolved
`FINDINGS` record without a correction or disposition.

- [Finding](https://github.com/cirwel/unitares/pull/2318#issuecomment-5735920343)
- [Correction and regression tests](https://github.com/cirwel/unitares/commit/796eceabf370d9fcc02cdb49a0b84c0630c93042)
- [Reviewer-identity limitation accepted in the same PR](https://github.com/cirwel/unitares/pull/2318#issuecomment-5736180200)

The corrected gate retains unresolved findings and separately permits a
disposed finding to clear. The mechanism still trusts records filed by a
GitHub account; it does not cryptographically prove which model performed the
review.

### A condition survived the original session and became a later repair — PR #2317

A structured review on earlier PR #2128 required that success in one chunk not
erase another chunk's failed scan. That review session ended later through
liveness cleanup with conditions still unresolved. Ten days after the review
opened, #2317 cited the session and implemented that condition.

- [Repair PR](https://github.com/cirwel/unitares/pull/2317)
- [Implementation and regression](https://github.com/cirwel/unitares/commit/f276984035b025d3d21856b1c5a1b4c7c06fd9da)

The regression drives a failed first region followed by a successful second
region and requires the first failure to remain visible. This is evidence that
a structured condition remained recoverable across sessions. The underlying
UNITARES transcript is operator-private, so public readers can inspect the PR's
citation and patch but cannot independently retrieve that transcript here.

## What this licenses

The record supports these statements:

- Agent review produced specific, consequential corrections in recent UNITARES
  development.
- Some review provenance, objections, dispositions, and unresolved conditions
  remained recoverable after the process that created them ended.
- UNITARES participated in routing or retaining some of those records.

It does not support these statements:

- UNITARES caused every correction.
- UNITARES review is better than ordinary code review or structured notes.
- Ten of twenty is a population rate, an incident-reduction estimate, or a
  productivity result.
- A named reviewer model proves independent control; several records were
  author-relayed and the same GitHub account filed them.
- Passing CI proves the corrected invariant is complete or holds in production.

One live record also demonstrates an instrument failure: a Codex route degraded
to a local model, an unparseable response became a disagreement with no
substantive reasoning, and the session later ended through inactivity cleanup.
PRs [#2321](https://github.com/cirwel/unitares/pull/2321) and
[#2322](https://github.com/cirwel/unitares/pull/2322) address parts of that
failure class. The failure belongs beside the successful traces because review
provenance is useful only when non-judgment, fallback, rejection, and terminal
cleanup stay distinct.

## Next measurement

Future records should bind each finding to the reviewed diff, reviewer
provenance, a concrete failure witness or source contradiction, disposition,
correction SHA, and validation that would fail without the change. UNITARES's
role should be explicit: routed the review, retained the condition, enforced a
gate, or no contribution established.

A later causal comparison should use a matched review budget and preserve clean
reviews, unresolved findings, cost, and downstream escapes. The fraction of PRs
edited after review is not a suitable optimization target because it rewards
unnecessary churn.
