# Dialectic reviewer labels — what antithesis messages actually do

Labels for every substantive non-canary `antithesis` message in
`core.dialectic_messages`, produced 2026-08-19. Data:
[`dialectic-reviewer-labels-20260819.jsonl`](dialectic-reviewer-labels-20260819.jsonl).

## Why this is a file and not a column

`core.dialectic_messages.agrees` exists and is **122 NULL / 1 false / 0 true** —
the reviewer verdict the schema asks for is essentially never recorded, so the
one signal you would most want to mine (did the reviewer actually disagree, and
was the disagreement any good) is not queryable.

The obvious move is to backfill it. That would be a mistake. Labels inferred by
reading are not the same claim as a verdict a reviewer set, and written into the
same column they become indistinguishable from one. A later reader could not
tell a first-party verdict from a 2026-08-19 inference, which is the exact
failure this repo keeps finding elsewhere: a value separated from its
provenance, read as more authoritative than it is.

So the labels live here, attributed, with `source_of_truth: false` on every row.
`agrees` stays NULL until a reviewer sets it.

## Why not a boolean

A boolean cannot express what reviewers actually do, which is likely *why* the
column was never filled. Five behaviours are distinguishable by reading, and
collapsing them loses the only distinction that matters:

| label | meaning |
|---|---|
| `refutes_substantive` | a specific, checkable objection that could change the conclusion |
| `concurs_with_conditions` | supports the direction, adds real constraints |
| `ratifies` | agrees; adds nothing that changes the outcome |
| `formulaic` | templated critique that does not engage this thesis's specifics |
| `non_verdict` | no judgment at all — the reviewer model failed to return one |

## Distribution (n=97)

| label | n | share |
|---|---|---|
| `refutes_substantive` | 41 | 42.3% |
| `formulaic` | 37 | 38.1% |
| `concurs_with_conditions` | 9 | 9.3% |
| `ratifies` | 6 | 6.2% |
| `non_verdict` | 4 | 4.1% |

## What this corrects

**⛔A naive `agrees=false` backfill would have been badly wrong.** Taking
"did not agree" at face value labels 82 of 97 as disagreement — reviewers
would look 85% adversarial. Only 42% substantively engage. The other 43% is
boilerplate and parse failure.

**⛔The rubber-stamp worry is the smallest failure mode, not the largest.**
The standing concern, stated inside the corpus itself, was that reviewers
"agreed, praised thoroughness, sharpened one condition, surfaced nothing new."
Measured: `ratifies` is 6 messages, 6.2%. The dominant failure is the opposite
shape — **templated pseudo-disagreement**. 37 messages apply a stock frame
("the proposed root cause is a symptom, not the systemic failure"; "classic
rationalization") without engaging what the thesis said. Nearly all come from
the local-LLM reviewer, and one repeated thesis
(*"the plumbing works; the only open question is reviewer quality"*) drew ~24 of
them in near-identical wording.

Boilerplate disagreement reads as rigour and scores as rigour. It is harder to
detect than sycophancy, and no count of disagreements will find it.

**⛔`non_verdict` rows are manufactured disagreement.** Four messages read
verbatim *"Reviewer model returned no parseable verdict; defaulting to
disagreement (no independent approval without a real judgment)."* The
fail-closed default is defensible as policy, but the row is then a parse
failure wearing a verdict's clothes. Any analysis that counts disagreements
must exclude these, and nothing in the schema marks them.

**Five duplicate pairs** carry byte-identical text under different
`message_id`s (54/195, 34/208, 38/212, 95/219, 102/225). Deduplicate before
computing any rate.

## Using this as an evaluation set

The usable subset is **41 substantive refutations**, not 97 messages and not
118 sessions. That is a benchmark, not a training corpus — see the sizing in
[`dialectic-lessons`](../../README.md) territory: the whole dialectic corpus is
~100k tokens, three orders of magnitude short of a fine-tuning set and roughly
the right size for a gold eval.

The strongest single item is the `#1387` measurability session (message 628),
where the reviewer produced three checkable refutations and the thesis author
conceded all three, writing *"two of them I could not have reached alone."*
That is the target behaviour; the 37 formulaic messages are the anti-target,
and having both labelled in one file is what makes the set discriminative.

## Method and limits

Manual read of `dialectic_messages.reasoning` for every non-canary antithesis
with >80 characters of reasoning; canary partition excluded via
`agents.label LIKE 'canary_dialectic%'` (`trigger_source` is not reliable for
this). Single labeller, no second pass, no inter-rater agreement — treat
category boundaries between `refutes_substantive` and `formulaic` as the
softest, since a templated frame occasionally lands on a real defect.
