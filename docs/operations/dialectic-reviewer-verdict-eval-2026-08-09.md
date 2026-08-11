# Dialectic reviewer-verdict evaluation — 2026-08-09

Status: evidence-only baseline for [#1585](https://github.com/CIRWEL/unitares/issues/1585). It changes no production state or routing.

## Decision

Do not turn every recorded reviewer rejection into a terminal `blocked` outcome.
In the frozen review cohort, 16 of 18 rejections (88.9%) describe a nonterminal
route: retry after evidence, cooldown, or human judgment. Only 2 of 18 are
compatible with an unqualified terminal denial.

This is a **terminal-denial mismatch rate**, not a ground-truth false-block
rate. None of the 18 cases has an independent correctness adjudication, so the
actual false-block rate remains unidentified. A later runtime change must first
represent `needs_evidence`, `cooldown`, and `human`/escalation separately from
`deny`; it must not use the current `agrees=false` bit as terminal-denial
authority.

## Frozen cohort

The source analysis completed at `2026-08-09T19:31:50Z`. The reproduction uses
that timestamp as an upper bound and `2026-06-28T06:00:00Z` (midnight MDT) as
the lower bound.

The population includes every dialectic session created within those bounds.
A reviewer verdict is a `synthesis` message authored by the session's persisted
`reviewer_agent_id`. The declared probe-family exclusion is preserved exactly:
exclude a session when its paused-agent label matches
`(probe|canary)|^RP[0-9]`, case-insensitively. The exclusion was not expanded
after outcomes were inspected; changing it requires a new cohort ID.

| Cut | Sessions | Verdicts | Reject | Approve | Missing |
| --- | ---: | ---: | ---: | ---: | ---: |
| Frozen population | 54 | 53 | 49 (92.5%) | 4 | 1 |
| After declared probe-family exclusion | 21 | 20 | 18 | 2 | 1 |

The selected cohort also reproduces the earlier timing checks: reviewer
antithesis to verdict was 42 ms median and 64 ms maximum. Thirteen of the 18
recorded rejections were followed by a paused-agent self-clear within the
frozen window (72.2%). That is an override/authority observation, not evidence
that either side was correct.

## Annotation rubric

Only the 18 recorded rejections receive one of the requested route labels. The
two approvals and one missing verdict remain in cohort accounting but are not
forced into a rejection category.

- `deny`: rejects the proposal as scoped without a bounded evidence, time, or
  operator route for the same request.
- `cooldown`: permits reconsideration after an explicit time or state threshold.
- `needs_evidence`: names concrete evidence, verification, corrective work, or
  an independent review required before reconsideration.
- `human`: lacks a parseable judgment or requires a normative/operator choice
  that the reviewer cannot settle deterministically.

One annotator reviewed the stored reviewer rationale and conditions. The
checked-in label file retains only cohort-scoped case pseudonyms, route labels,
confidence, and coarse basis codes. It contains no raw session IDs, agent
labels, topics, prompts, conditions, or transcript prose. The pseudonyms remain
linkable by an operator with source-database access and therefore are not
anonymization.

## Results

| Route label | Count | Share of recorded rejections |
| --- | ---: | ---: |
| `deny` | 2 | 11.1% |
| `cooldown` | 1 | 5.6% |
| `needs_evidence` | 11 | 61.1% |
| `human` | 4 | 22.2% |

Derived routing quantities:

- Terminal-denial mismatch: 16/18 (88.9%).
- Retry/evidence route (`cooldown + needs_evidence`): 12/18 (66.7%).
- Direct operator route (`human`): 4/18 (22.2%).
- Independently adjudicated for block correctness: 0/18; false-block rate is
  not identified.

Three of the four `human` routes are fail-closed responses to an unparseable
reviewer verdict. The other requires a normative architecture choice and
stakeholder agreement. This distinction matters operationally: parser failure
should trigger bounded reviewer recovery or reassignment before consuming a
human slot when that can be done safely.

## Reproduction

Run against the local `governance` PostgreSQL database:

```bash
python3 scripts/dev/dialectic_verdict_labels.py --verify-baseline
python3 scripts/dev/dialectic_verdict_labels.py --verify-baseline --json
```

`--verify-baseline` fails on any denominator, outcome-count, self-clear, or
latency mismatch. New sessions after the cutoff cannot alter the result.

## What this does not establish

- Route semantics do not establish whether a reviewer was substantively right.
- A later self-clear does not prove a false block; the paused agent was not an
  independent adjudicator.
- This is a small, operationally selected cohort with one annotator and no
  inter-rater reliability estimate.
- The result does not authorize automatic resumption. It only rejects the
  opposite overreach: treating every Boolean rejection as terminal denial.

The next evidence step is independent operator adjudication of
`false_block | justified_block` against the underlying evidence and eventual
outcome. Until coverage is complete, the evaluator deliberately reports the
false-block rate as unidentified.
