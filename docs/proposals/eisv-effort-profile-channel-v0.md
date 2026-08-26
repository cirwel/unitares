# EISV effort-profile label channel — reopening premise, pre-declared

**Status: DRAFT / design-first. Not a change, not a registration, and not a
read.** No code, no database access, and no live-path wiring is proposed here.
The document exists to fix a reopening premise in writing *before* the
2026-12-01 stop-rule read resolves, so the premise cannot be reverse-engineered
from that read's outcome. It becomes registrable only when the operator
declarations in §11 are filled; until then every threshold below is a proposal
to the operator, not a method.

Relationship to [`eisv-outcome-grounding-stop-rule-v0.md`](eisv-outcome-grounding-stop-rule-v0.md):
that rule owns the maintainer-deployment outcome question and its fixed
2026-12-01 confirmatory read. **Nothing here reopens, anticipates, substitutes
for, or informs it.** The interlocks are in §3 and are the load-bearing part of
this document.

Companion to [`eisv-grounding-next-move-v0.md`](eisv-grounding-next-move-v0.md)
(the design tournament that named bad-label supply as the binding constraint)
and [`independent-operator-cohort-preregistration-v0.md`](independent-operator-cohort-preregistration-v0.md)
(which addresses a different gap — external validity, not label supply).

## 1. Why this exists now

The stop rule's FAIL branch closes outcome-grounding and states the only way
back in:

> Reopening requires a *new premise* — a materially different label channel or
> measurement process — not simply more of the same labels.

That clause is currently unfilled. If it is still unfilled on 2026-12-02 and the
read FAILs, whatever gets proposed then will have been designed by someone who
has already seen how the question failed. The reopening premise would be
selected to fit the failure, which is the same defect the stop rule was written
to prevent one level down. Writing the premise now, under embargo, is the only
way it can be blind.

This is therefore a document whose *timing* is its main claim to rigor. Its
content may well be wrong; being on the record before the read is what makes it
checkable.

## 2. What the premise is

On 2026-08-26 Anthropic published
[Enabling independent research on how people use Claude](https://www.anthropic.com/research/enabling-independent-research),
describing a pilot in which three external groups ran independent studies on
aggregate Claude usage data. One of the three, METR, estimated real-world
productivity effects of coding agents by having a model judge **how long each
task would have taken without AI**, then comparing those judgments against known
completion times from a prior developer study before relying on them.

Two properties of that method are what this document borrows. Neither is the
result — METR's writeup was not public at the time of writing and its findings
are described as preliminary, so nothing here rests on their numbers.

**(a) The outcome variable is continuous and universal, not a scarce minority
class.** The stop rule's dependent variable is `is_bad`: a rare, externally
verified failure. Power is set by the bad count, the bad count is set by the
world (operator correction bandwidth, a mostly-non-committing fleet), and the
tournament's own arithmetic says the resulting gate is unwinnable at this
supply. A per-session *effort profile* has no minority class. Every session
that produced a transcript carries one. Power comes from the total, and
the total is not bandwidth-limited.

**(b) The judge is validated against known ground truth before it is used.**
METR did not assume the model's estimates were sound; they checked them against
measured developer times first. That validation step, not the estimation, is the
transplantable contribution, and it is the step this repo's measurement-authority
rules would demand anyway.

## 3. Interlocks with the registered 2026-12-01 read — frozen now

These bind on merge of this file and are not amendable by a later revision of
it:

1. **No phase of this channel touches `audit.outcome_events`, `core.agent_state`,
   EISV values, prior-state snapshots, or any discrimination statistic before
   the registered read is posted.** Phase 1 (§7) is the only phase that may run
   before then, and it is confined to transcripts and operator adjudication.
2. **No result from this channel is published between 2026-11-15 and the posting
   of the 2026-12-01 stop-rule read**, mirroring the scheduling embargo the
   independent-operator cohort protocol adopted, so neither read can frame the
   other.
3. **Results are never pooled.** An effort-ratio result and a bad-outcome
   discrimination result are different dependent variables on different
   populations. They are reported separately and never combined into one claim
   about "EISV predicting outcomes."
4. **A PASS here does not resurrect the registered question.** If the 2026-12-01
   read FAILs, the bad-outcome discrimination question stays closed on its own
   terms. This channel opens an adjacent question; it does not appeal the
   verdict on that one.
5. **Any implementation must fail closed before data access**, in the manner the
   stop rule now requires of the ablation CLI. The 2026-08-23 protocol-deviation
   audit records automation exposing live discrimination results every six hours
   after a frozen cutoff. A new channel that could be scheduled is a new instance
   of that hazard, and the burden is on the implementation to make the unsafe
   call impossible rather than discouraged.

## 4. What this is not

- **It is not a widening of the label definition.** The classifier gating
  `test_failed` is untouched, no existing row is reclassified, and no
  `verification_source` changes tier. The stop rule's "do not widen the label
  definition to raise the count" prohibition is about the bad-class definition
  and remains in force; this channel does not produce bad labels at all.
- **It is not the de-scoped resident-adjudication throughput dial.** The
  tournament de-scoped that on contamination grounds — an adjudication dial
  creates pressure to manufacture labels, and the 497-synthetic incident is what
  that looks like. An effort profile has no dial to turn. It is computed
  retrospectively over sessions that already happened, at whatever rate they
  happened; there is no rate to increase and therefore nothing to inflate. This
  distinction is the whole of the rebuttal, and if it turns out to be wrong the
  channel should be abandoned rather than repaired.
- **It is not the operator-correction label leg** the tournament recommended
  building. That leg targets the bad class and remains the standing
  recommendation, unaffected by this document.
- **It is not a live-path signal.** Eval-only, forever, unless a separate
  proposal argues otherwise against the Axiom-2 objection in §6.

## 5. The measurement

**Corrected 2026-08-26 — the dependent variable is observable, not
counterfactual.** An earlier revision made `t_counterfactual` — how long the work
would have taken without agent assistance — the numerator of the dependent
variable, and proposed validating it against the operator's own blind estimate.
That design is withdrawn. Its reasoning is in §5a; it is kept visible rather than
quietly replaced because the mistake is instructive.

For a completed agent session *s* with a transcript, all observable, no judge
required for any of them:

- `t_actual(s)` — wall-clock working span from transcript message timestamps.
- `turns(s)` — the number of exchanges.
- `cycles(s)` — re-direction events: the agent's output is rejected, corrected,
  or re-specified rather than carried forward.

Together these are an **effort profile**, not an efficiency score. The judge's
job is reduced to extracting `cycles(s)` from a redacted transcript, which is a
classification over text present in the artifact and therefore checkable. It no
longer estimates anything counterfactual.

The research question this channel would eventually ask is *not* the registered
one. It is: **does prior-state EISV carry information about the effort profile
of the session that follows it, over and above a per-agent persistence
baseline?**

The baseline clause is not optional. The tournament's central negative finding
is that a per-agent AR(1)/persistence null is the thing to beat, and that
beating a fleet mean alone is not success. An effort-ratio model that only beats
a fleet mean is dressed-up autocorrelation on a new axis, and the individuality
precedent applies unchanged.

**The stop rule's condition 3 does not transfer.** `Bad clusters ≥ 150` is a
support condition for a minority-class AUC read. A continuous outcome needs its
own support and power criteria, on its own permutation structure, declared
before any read. Reusing 150 here would be a number laundered across
questions — exactly the conflation the stop rule's "two counts" section exists
to prevent.

## 5a. Why the counterfactual and the operator-oracle were removed

The withdrawn design had the operator estimate `t_counterfactual` blind to the
judge, and treated agreement between the two as the validation statistic. Two
independent objections, either sufficient:

**The operator is not an oracle, and this is the one quantity where that is
proven.** METR's own earlier randomized trial of experienced open-source
developers is the canonical result here: measured **19% slower** with AI tools,
while the same developers self-reported **20% faster** — and the misestimate
survived doing the tasks. Forecasts before, self-assessment after, and
measurement disagreed in the same direction. Counterfactual duration is
precisely the judgement humans are documented to get wrong. Building this
channel's validation on an operator's blind estimate of it would have been
citing METR for the method while using the instrument METR's own work
discredited.

**It re-seats the operator as a throughput bottleneck.** The design tournament's
diagnosis of the label supply problem was that labels are limited by a solo
operator's correction bandwidth. A protocol that requires per-sample operator
adjudication spends the exact resource the channel was supposed to stop
depending on, and works against the direction
[`bridge-dispatch-v0.md`](bridge-dispatch-v0.md) is pushing — operator as
evidence-backed exception handler, not as transport.

**What replaces it.** Nothing, on the counterfactual: a quantity with no
observable ground truth and no trustworthy adjudicator is not measured here at
all. The effort profile in §5 is observable end to end.

Retaining the counterfactual as an *exploratory* secondary is permitted only
under a reliability floor and an explicit refusal of any validity claim:
heterogeneous judges from different model families — the routing
`UNITARES_DIALECTIC_REVIEWER_HOST` already provides — estimate independently,
and their agreement is reported as inter-family reliability. **Reliability is not
validity.** Judges that agree can be agreeing and wrong, and on this quantity
they would likely be wrong together, since they share the training distribution
that produced the human bias. Any published exploratory counterfactual carries
that sentence next to it.

**Operator authority is untouched by this.** The distinction the withdrawn
design collapsed is between the operator as *decision-maker* and the operator as
*measuring instrument*. Deciding standards — thresholds, effect sizes, what a
result licenses — belong to the operator and cannot be delegated to an
implementing agent; that is a standing rule in this repo and §11 keeps every one
of those slots. Supplying measurements is a different job, and this protocol no
longer asks for it.

## 6. Construct validity — the threats, worst first

**T1. Friction may be productive, which breaks the sign of the metric.** The
same Anthropic post carries the Stanford SALT Lab finding that friction in
human-AI collaboration is *often productive*: time spent seeing how the model
attempted a task, locating where intent was unclear, and re-directing is
reported as what produces the better result. If that holds here, a low effort
ratio is not unambiguously good and a high one is not unambiguously bad. The
metric would then be measuring speed while being interpreted as value.

This is the threat that can kill the channel outright, and it is prior to every
statistical concern below. **No effort-ratio read may be published without a
declared position on it.** The honest minimum is to report the ratio as
*throughput telemetry* and refuse any efficiency-is-goodness reading, in the
same posture Φ already holds (telemetry-only, no verdict authority).

**T2. Axiom-2 / anti-RLHF.** A speed metric wired to a verdict hard-codes
optimize-toward-fast, the same defect the tournament identified in the
recent-failure-rate gate (punish-toward-zero-failures). The mitigation is
structural, not procedural: eval-only, never read by the verdict path.

**T3. Loop contamination through the transcript — Invariant 4.** This is the
sharpest repo-specific hazard and it is easy to miss. Governance output is
surfaced *into agent context* by the envelope middleware, so a raw session
transcript contains the loop's own verdicts, metrics, and state commentary. A
judge reading that transcript would be estimating effort partly from the
governance loop's own output, and a signal derived from the loop cannot anchor
the loop. **Redaction of all governance-envelope content from judge input is a
correctness requirement, not a hygiene nicety**, and the redaction must be
verified by a test that fails on leakage rather than asserted in a docstring.

**T4. Judge wording sensitivity.** Anthropic's own account of the pilot names
this as the failure mode they could not fully solve: the tool "is sensitive to a
question's wording; a poorly phrased one can place conversations into categories
that misrepresent them," and — their words — "because no one can read the
underlying conversations, these errors are hard to catch." Their workaround,
piloting questions on a public corpus, failed in the predictable direction:
prompts that behaved on WildChat produced misleading categories on real traffic.
This repo's version of that lesson is already written down as the four states a
zero cannot distinguish. Here the mitigation is available and theirs was not:
the transcripts *can* be read, by the operator, on a sample. That is Phase 1.

**T5. Corpus heterogeneity.** The transcript corpus is harness-local and
per-harness heterogeneous (see Phase 1). A judge prompt calibrated on one
harness's format is not validated for another, and the substrate-plurality
posture this repo already holds elsewhere applies: the harness is a property of
the reading, not a nuisance parameter to average over. Phase 1 results are
reported per harness or not at all.

**T6. Selection.** Sessions that produce transcripts are not a random sample of
work. Whatever the coverage turns out to be, it is reported as a coverage
statement, and the population is described by what it is rather than called "the
fleet."

## 7. Phases

The ordering below is chosen for blinding, not efficiency. Phase 1 runs before
Phase 0 even though that risks validating a judge for a channel that later
proves to have no supply. That cost is accepted deliberately: Phase 1 is the
part that must not be able to see the December read, and its cost is bounded to
one sample adjudication.

**Phase 1 — judge validation (may run now).** No database, no EISV, no outcome
events. Two sub-reads:

- *1a, checkable:* the judge estimates `t_actual` from a redacted transcript with
  all timestamps stripped. Ground truth is the recorded wall-clock. This is the
  local analogue of METR's check against known developer times, and it is fully
  verifiable without any new labelling effort.
- *1b, checkable:* the judge extracts `cycles(s)` — re-direction events — from
  the same redacted transcript. Ground truth is a rule-based extraction over
  explicit correction markers, which is conservative (it under-counts implicit
  re-direction) and is therefore reported as a floor, not as a gold standard.
  Disagreement is inspected by reading the transcript, which is the mitigation
  Anthropic's pilot could not use and this one can.

Neither sub-read asks anyone to estimate a counterfactual, and neither requires
operator adjudication. Both compare a judge's output against something already
present in the artifact. Both must clear their declared thresholds.

*Where the corpus actually lives (corrected 2026-08-26).* An earlier revision of
this section assumed the transcripts and the wall-clock were server-side. They
are not. The governance database holds no agent-session transcript: the only
`transcript` it stores is the dialectic message record
(`src/dialectic_protocol.py`), which is a record of a review conversation, not of
an agent working. Session transcripts are **harness-local files** — the Claude
Code project store, Codex's own store — which this repo does not own and whose
formats differ per harness. `t_actual` comes from transcript message timestamps
for the same reason: `audit.tool_usage` spans only governance calls, so it is a
lower bound on working time, it covers only agents that call governance tools,
and it carries a known transport blind spot (#1424, `/mcp` and `/sse` unrecorded).

This cuts both ways and both directions matter:

- It makes Phase 1 **genuinely embargo-safe** under §3.1 rather than
  safe-by-promise. The phase reads files on the operator's machine and cannot
  reach `audit.outcome_events` or `core.agent_state` even by mistake.
- It makes Phase 1 **harness-coupled**, which the rest of this document does not
  reflect. Any implementation needs a per-harness reader, the sample definition
  has to state which harness it drew from, and a result on one harness does not
  transfer to another. This is a portability cost the proposal had not paid.

**Phase 0 — supply and coverage census (deferred past the registered read).**
Counts only: how many sessions are joinable to a prior-state snapshot, how they
distribute across the configured roster, and what permutation structure they
induce. This requires the database and is therefore embargoed by §3.1. A naive
supply count would also be misleading in the same way the stop rule warns about:
labels are not blocks, and more rows on the same snapshots add no permutation
structure.

**Phase 2 — registered read.** Only after Phases 1 and 0 clear their declared
gates, and only under a separate registration document with its own fixed date,
cohort, thresholds, and power declaration. This document does not register it
and must not be cited as having done so.

## 8. The judge must run free

The execution-cost policy governs the instrument here, not just the deployment.
The primary arm's judge backend must be a local, unmetered model — the same
Ollama path `src/verification_backend.py` already uses — so that an operator with
no model budget can execute the entire protocol. A metered backend is welcome as
a config-gated, off-by-default secondary arm, reported separately.

The honest consequence, stated in advance so it cannot be discovered
conveniently later: **a local model may simply fail Phase 1.** METR used a
frontier model. If the free backend cannot clear the declared agreement
threshold and a metered one can, the correct conclusion is that this channel is
an opt-in capability for funded operators and **cannot become part of the core
path**. It does not become a reason to put a metered dependency on the required
path, and it does not become a reason to lower the threshold.

The backend is part of the instrument. The registered backend, model version,
and prompt text are frozen at registration; changing any of them invalidates the
validation and requires re-running Phase 1, not a footnote.

## 9. Measurement authority

Everything the standing rules say applies here, with two consequences worth
making explicit because this channel is unusually easy to misuse:

- A judge-derived count may not retire any capability. If effort profiles come
  back flat, that is telemetry about the ratio, and it authorizes nothing about
  EISV, the sessions measured, or the agents that produced them.
- **A failed Phase 1 kills the judge as an instrument, not the question.** "The
  local model could not estimate effort from redacted transcripts" and "prior
  state carries no information about effort" are different findings and must
  never be reported in the same sentence. This is the second of the four states
  a zero cannot distinguish — not reachable — and it is the one this design is
  most likely to land in.

## 10. Fleet neutrality

Per-agent judge calibration is a legitimate N=1 partition key: each named
resident is its own class and the name selects a scale constant. It is never a
dispatch key. No phase may branch on a resident name, and the roster is read
from configuration. The residentless install is the default case and any
implementation needs its companion assertion for an empty roster.

## 11. Open operator declarations

None of the following may be chosen by an implementing agent, and none may be
chosen after seeing data. A threshold that turns a measurement into a verdict is
a judgement call and belongs to the operator. The bracketed values are proposals
offered for rejection, not defaults.

| # | Declaration | Proposed | Status |
|---|---|---|---|
| D1 | Phase 1a agreement threshold (judge vs recorded wall-clock) | [Spearman ρ ≥ 0.6 on ≥ 60 sessions] | **UNFILLED** |
| D2 | Phase 1b agreement threshold (judge vs rule-based cycle floor) | [ρ ≥ 0.5 on ≥ 60 sessions] | **UNFILLED** |
| D3 | Registered primary judge backend, model version, prompt | [local Ollama; version pinned at registration] | **UNFILLED** |
| D4 | Smallest relevant effect for the Phase 2 read | — | **UNFILLED** |
| D5 | Phase 2 read date and cohort | — | **UNFILLED** |
| D6 | Position on T1 (productive friction) required before publishing | — | **UNFILLED** |

Every slot above is a *decision*, not a measurement. None of them asks the
operator to supply a number about the world; each asks what standard a number
must meet. That is the only role this protocol assigns to the operator, and it
is the one role that cannot be delegated.

D4 deliberately mirrors the slot the stop rule already records as unfilled. A
channel that reaches a read without it earns `INCONCLUSIVE`, by the same rule
that governs the December read.

## 12. Do not

- Do not run any phase of this before the 2026-12-01 read except Phase 1, and do
  not let Phase 1 touch the database.
- Do not cite this document as a registration, a gate, or evidence that the
  reopening clause has been satisfied. It pre-declares a premise; satisfying the
  clause requires Phases 1 and 0 to clear and a separate registration to exist.
- Do not present an effort profile as a measure of value, quality, or agent
  performance. Absent a declared position on T1 it is a speed reading and
  nothing else.
- Do not reuse `Bad clusters ≥ 150`, the withdrawn `0.05` AUC bound, or any
  other number from the bad-outcome track as a threshold here.
- Do not schedule any part of this. The 2026-08-23 audit exists because
  automation ran a read 93 times that a human would have run once.
- Do not lower a Phase 1 threshold because the free backend missed it.
- Do not reintroduce an operator estimate, self-report, or recollection as
  ground truth for any quantity in this protocol. If a quantity has no
  observable referent, it is exploratory or it is not measured — see §5a.

## 13. Provenance

The method borrowed in §2 is described in Anthropic's
`enabling-independent-research` post of 2026-08-26, attributed there to METR,
whose own writeup was not public at the time of writing and whose findings that
post describes as preliminary. The productive-friction threat in T1 is from the
Stanford SALT Lab study reported in the same post. The judge-wording failure
mode in T4 is Anthropic's own account of running the pilot. The earlier METR
randomized trial cited in §5a — measured 19% slowdown against a self-reported
20% speedup among experienced open-source developers — is the published result
that removed the operator-oracle design; it is load-bearing for that removal and
for nothing else. None of these are
peer-reviewed results and none are load-bearing for anything asserted here; they
are the source of a design, not evidence for it.

Repo antecedents: the binding-constraint diagnosis and the de-scoped
adjudication dial are from `eisv-grounding-next-move-v0.md`; the anchor tiering
and Invariant 4 are `src/grounding/outcome_anchors.py`; the supply census this
channel's Phase 0 would extend is `scripts/analysis/eisv_latent_label_supply.py`;
the local-model backend precedent is `src/verification_backend.py`.
