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

## ⛔Do not read the pooled distribution — it straddles an instrument change

The reviewer backend changed on **2026-07-02**, when
`UNITARES_DIALECTIC_REVIEWER_HOST=codex` was activated on the agent-orchestrator
and the local gemma4 model became the degraded fallback only
(see the dialectic-rework record). Pooling across that boundary averages two
different instruments:

| label | pre 07-02 (n=76) | on/after 07-02 (n=21) |
|---|---|---|
| `refutes_substantive` | 24 — 31.6% | **17 — 81.0%** |
| `formulaic` | **36 — 47.4%** | 1 — 4.8% |
| `concurs_with_conditions` | 8 — 10.5% | 1 — 4.8% |
| `ratifies` | 5 — 6.6% | 1 — 4.8% |
| `non_verdict` | 3 — 3.9% | 1 — 4.8% |

Pooled, for reference only: 41 / 37 / 9 / 6 / 4 out of n=97.

**The templated-critique failure is a pre-fix artefact.** It collapses from
47.4% to 4.8%, and substantive refutation rises from 31.6% to 81.0%. Quoting
the pooled 38.1% as the current state would be exactly the stale-baseline error
the measurement-authority contract exists to prevent.

**⛔The improvement is NOT cleanly attributable to the backend change.** The
post-07-02 population is not a random sample of the pre-07-02 one: there are far
fewer sessions and they skew toward deliberate operator-posed theses, whereas
the pre-fix corpus is dominated by one repeated automated thesis that alone drew
~24 near-identical stock frames. **The theses changed, not only the reviewer.**
Separating the two needs a matched control or random assignment across hosts;
this data has neither, so the honest reading of the collapse from 47.4% to 4.8%
is "some mixture of a better reviewer and an easier, smaller, differently-shaped
question set", with the split between them unmeasured.

That objection was raised by the orchestrated Codex reviewer itself in dialectic
session `def32eb2b4b2ce93` (2026-08-19), an adversarial self-test in which this
document's own claim was submitted as a thesis. It was deliberately withheld
from the thesis as an answer key and the reviewer found it unprompted.

**Further limits.** n=21 is small, so 81% carries a wide interval and the 4.8%
formulaic figure is literally one message. The split is by DATE as a proxy for
backend: no per-message model attribution existed until PR #1725 (2026-08-18),
so no row here records which model actually answered. Every antithesis on/after
07-02 did come through the orchestrated reviewer path (43/43), but that path also
existed before, on gemma4 — the path name is not the discriminator, the
activation date is.

**These labels are single-labeller, unblinded, and authored by the same party
that argued from them.** No second pass, no inter-annotator agreement. The
boundary the claim rests on — `refutes_substantive` versus `formulaic` — is the
one the labeller most controls. Any closure decision needs blind double-labelling
with reported agreement, and `non_verdict` carried as an explicit denominator
category so a refutation rate cannot drift upward without someone choosing to
move it.

(Aside: the rework record's 2026-07-25 note that there was "exactly 1 dialectic
session since Jul-02, zero organic use" is itself now stale — 21 labelled
antithesis messages fall on or after that date.)

## What this corrects

**⛔A naive `agrees=false` backfill would have been badly wrong.** Taking
"did not agree" at face value labels 82 of 97 as disagreement — reviewers
would look 85% adversarial. Pooled, only 42% substantively engage.

**⛔Rubber-stamping was never the dominant failure.** The standing concern,
stated inside the corpus itself, was reviewers who "agreed, praised
thoroughness, sharpened one condition, surfaced nothing new." `ratifies` is 6
messages across the whole corpus and never exceeds 6.6% in either era. The
failure that actually dominated the pre-fix corpus was the opposite shape —
**templated pseudo-disagreement**: 36 messages applying a stock frame
("the proposed root cause is a symptom, not the systemic failure"; "classic
rationalization") without engaging what the thesis said. One repeated thesis
(*"the plumbing works; the only open question is reviewer quality"*) drew ~24 of
them in near-identical wording.

Boilerplate disagreement reads as rigour and scores as rigour. It is harder to
detect than sycophancy, and **no count of disagreements will find it** — which
is why the dialectic-rework record predicted a rubber-stamp would push a
resolution-rate metric green while quality went to zero. The polarity was
inverted from the prediction; the mechanism was the one predicted.

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

**Size first: this is an evaluation set, not training data.** The whole
dialectic corpus is roughly 100k tokens — three orders of magnitude short of a
fine-tuning set, and about the right size for a gold eval.

Within it, the usable subset is the **41 substantive refutations**, not 97
messages and not 118 sessions. And if the question is what the CURRENT reviewer
does, the relevant subset is the **17 post-07-02 refutations**, which is small
enough that the honest use is as a seed, not a benchmark.

The strongest single item is the `#1387` measurability session (message 628),
where the reviewer produced three checkable refutations and the thesis author
conceded all three, writing *"two of them I could not have reached alone."*
That is the target behaviour; the 37 `formulaic` messages (36 of them pre-fix)
are the anti-target, and having both labelled in one file is what makes the set
discriminative — a scorer that cannot separate message 628 from the stock
"classic rationalization" frame is not measuring review quality.

## Method and limits

Manual read of `dialectic_messages.reasoning` for every non-canary antithesis
with >80 characters of reasoning; canary partition excluded via
`agents.label LIKE 'canary_dialectic%'` (`trigger_source` is not reliable for
this). Single labeller, no second pass, no inter-rater agreement — treat
category boundaries between `refutes_substantive` and `formulaic` as the
softest, since a templated frame occasionally lands on a real defect.
