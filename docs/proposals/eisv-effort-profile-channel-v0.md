# EISV effort-profile label channel — separate track, refuted as designed

**Status: SEPARATED AND REFUTED, 2026-08-26.** This document was written as a
pre-declared reopening premise for the outcome-grounding stop rule. It is no
longer that, on two counts decided the day it was written:

1. **D7 was answered `separate track`** (§5). The channel does not address the
   registered question's concern, so it has no claim on the reopening clause.
   That clause stays **empty** through the 2026-12-01 read.
2. **The design is refuted as written** (§0). Three independent adversarial
   passes found two circularities that are each independently fatal, plus a
   pre-committed kill check that could not fire.

What remains is a proposal for a **new track that must earn its own
justification from scratch**, and which currently does not. Read §0 first. Do
not cite this document as a reopening premise, a registration, a gate, or
evidence for anything.

An earlier revision of this header read "No code, no database access, and no
live-path wiring is proposed here." That was true when written and false by the
end of the same commit, which added `scripts/analysis/effort_profile_degeneracy.py`
and its tests. The sentence is corrected rather than deleted because the
document's own self-audit sentences turning out to be unreliable is part of what
§0 records.

Relationship to [`eisv-outcome-grounding-stop-rule-v0.md`](eisv-outcome-grounding-stop-rule-v0.md):
that rule owns the maintainer-deployment outcome question and its fixed
2026-12-01 confirmatory read. **Nothing here reopens, anticipates, substitutes
for, or informs it.** The interlocks are in §3 and are the load-bearing part of
this document.

Companion to [`eisv-grounding-next-move-v0.md`](eisv-grounding-next-move-v0.md)
(the design tournament that named bad-label supply as the binding constraint)
and [`independent-operator-cohort-preregistration-v0.md`](independent-operator-cohort-preregistration-v0.md)
(which addresses a different gap — external validity, not label supply).

## 0. Refutation record — three adversarial passes, 2026-08-26

The passes were conceptual refutation, ground-truth verification against the
live system, and stranger-executability, matching the review
[`independent-operator-cohort-preregistration-v0.md`](independent-operator-cohort-preregistration-v0.md)
ran before registering. They converged.

**These are refutations, not threats.** The distinction is load-bearing and the
first pass predicted it would be violated — that the response would be to add
"T8 through T12" to §6 and continue. A threat is something a design will handle;
a refutation is a finding that the design fails. Nothing below is being managed.

### R1. The predictor is made of the outcome — fatal

`CLAUDE.md` records that the live per-turn `process_agent_update` is "a
substrate reading of turn shape." `src/behavioral_state.py` makes behavioral
EISV an EMA over those updates, with `update_count` driving confidence through
`BOOTSTRAP_UPDATES` and `BASELINE_WARMUP_UPDATES`. So prior-state EISV is a
smoothed function of past turn shape — and §5 proposed predicting *this
session's turn shape* from it.

A positive Phase 2 read would be fully explained by EMA-of-past-turn-count
predicting next-turn-count, with no grounding content. §5's per-agent
persistence baseline does not save this: it removes autocorrelation in `y`,
while `X` is *constructed from* `y`'s own history by live middleware. §4
pre-committed that if its rebuttal failed the channel "should be abandoned
rather than repaired." This is that failure.

### R2. The other half of the dependent variable measures the loop too — fatal

`t_actual` is a `max − min` span over transcript timestamps, so it contains
every governance call's latency. `CLAUDE.md`'s Substrate Tax section documents
in-handler KG calls at "~4,464ms, a ~60× amplification," and the plugin's
`hooks/post-stop` fires `process_agent_update` every turn. Duration is
therefore partly a measurement of the governance loop's own cost. Both
components of the effort profile are entangled with the thing they were
supposed to be independent of.

### R3. §14's authority split does not survive its implementation

§14 grants KILL authority to `turns` and `t_actual` *because* they are
judgement-free, and withholds it from `cycles` because a marker set is a
judgement. But `_is_human_turn` embeds four judgement calls, and measured on
the only real corpus, 12 of 22 counted "human turns" are harness-injected
`task-notification` wakes — **55% inflation** on the quantity holding kill
authority. Injected `system-reminder` blocks arrive as `type: "attachment"`
records the classifier never sees.

Compounding it, `read_session` silently drops zero-turn and single-timestamp
sessions. That is an undeclared selection rule removing the *shortest* sessions,
which biases the corpus away from `median turns ≤ 2` — the kill condition. Under
§14's own logic `turns` as implemented belongs on the `cycles` side of the line.

### R4. The pre-committed thresholds could not fire

`CV < 0.25` on session durations, `IQR == 0` on turn counts, and
`median turns ≤ 2` do not require seeing a distribution to rule out; they
describe a corpus that is not agent sessions. §14 presented not having seen
*this* corpus as blinding. Not having seen the corpus is not the same as not
knowing what the quantity looks like. The check was offered as the answer to the
objection that this document is unfalsifiable, and it was a test designed to
pass.

### R5. The marker set scores zero precision and zero recall

Measured against real text: `"stop "` matches "the stop rule", `"instead"`
matches ordinary repo idiom, `"no,"` and `"don't"` match an explicit agreement.
Meanwhile the operator turns that actually re-directed this work — including the
one that caused §5a's withdrawal — match nothing. On the only corpus available
the single counted cycle came from pasted third-party prose. `MARKER-SET-BLIND`
only fires on *under*-firing, so the guard cannot detect this instrument's
actual failure mode.

### R6. §1's blindness claim is false

The stop rule publishes its own likely failure: 28–29 observed blocks against a
threshold of 150, "arithmetic gaps of 121 and 122 blocks," and the tournament
records the target as needing "a ~17× sustained rate increase the system cannot
*cause*." A condition-3 FAIL is the forecastable default. §2(a) — "no minority
class" — is written directly against minority-class scarcity, which is that
failure by name. The document is not blind to how the question fails; it is
written from it.

### R7. §2(a)'s power claim is refuted by §7 of this document

§2(a) says power comes from the total. §7 says "labels are not blocks, and more
rows on the same snapshots add no permutation structure." Effective power is set
by distinct prior-state snapshots, of which the frozen matrix reports 28–29. A
continuous outcome adds rows within blocks, not blocks. The escape from the
binding constraint is not an escape; it relocates which count is scarce and
never argues the new one is abundant.

### R8. Three factual errors

- **#1424 is the fix, not the defect.** `src/audit_db.py` sets
  `TOOL_USAGE_MCP_INSTRUMENTED_SINCE = 2026-07-31T19:24:49Z`; the blind spot is a
  historical boundary, not a standing property. §7 cited the remedy as the
  disease. Corrected below. The conclusion it supported — that Phase 1 must be
  harness-local — survives on the two other reasons.
- **"93 times" was never in the audit.** The rows are 51 fired / 42 completed
  and 52 fired / 43 completed: 103 and 85. The 93 is one job's fired count plus
  that same job's completed count. Corrected in §12.
- **The dialectic pointer was wrong.** The stored artifact is
  `core.dialectic_messages`, not the in-memory list in `src/dialectic_protocol.py`.
  The substance — that the governance database holds no agent-session
  transcript — was verified across all migrations and stands.

### What survived the passes

§5a's withdrawal of the operator-oracle and its METR citation. §9's
instrument-not-question rule. §8's free-backend requirement including its
unwelcome branch. §3.5's fail-closed requirement. §5's refusal to reuse
`Bad clusters ≥ 150`. The script's isolation claims, which are verifiable in
ten seconds. And §3's frozen interlocks, which were right — see §5.

### What would have to be true for this track to resume

R1 and R2 answered, not mitigated: a dependent variable demonstrably not
constructed from the predictor's inputs. Nothing else in this document matters
until that exists.

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

**The standing objection to this whole document, stated plainly.** If the
effort profile is a different question, then calling it a reopening premise for
outcome-grounding may be a category error rather than a contribution: a
measurement problem solved by quietly changing the subject. The stop rule's
clause asks for a different *measurement process* for the same concern, not a
different concern. Whether "does prior state predict how a session goes" is the
same concern as "does prior state predict externally-verified bad outcomes" is
a judgement, and it is the operator's, not this document's. It is listed as D7
in §11. If the answer is that they are different concerns, this document is not
a reopening premise and should be read as a proposal for a new track that must
earn its own justification from scratch.

**Answered 2026-08-26: SEPARATE TRACK.**

An earlier answer the same day said "same concern," on the reading that the
registered question is about whether EISV is grounded in anything outside the
loop and that bad outcomes were merely the available signal. That answer is
**reversed**, and the reversal is recorded rather than overwritten because how it
was reached matters more than the answer.

*Why it was reversed.* The concern question itself is not verifiable — what a
stop rule was *for* is intent, not fact. But the justification offered for "same
concern" is verifiable, and it fails two independent checks:

- **Its own criterion, its own instrument.** "Outside the loop" is not a loose
  phrase in this repo; `src/grounding/outcome_anchors.py` implements it.
  `external_signal` is TRUSTED_EXTERNAL, `server_observation` is EXCLUDED as
  "the loop's self-validation — the one that would silently build the echo
  chamber if treated as an outcome," and unlisted provenance is EXCLUDED by
  default. An effort profile is derived from a governed session's transcript and,
  per R1 and R2, from quantities entangled with the loop's own operation. It is
  not `external_signal`. Under the criterion that was offered to justify "same
  concern," the channel fails.
- **A textual incompatibility.** The registered PASS branch reads
  "outcome-grounding remains open, *and Stage B may be reconsidered*." Stage B is
  actuation. §4 declares this channel "not a live-path signal. Eval-only,
  forever," and T3 requires that an effort profile "must never enter agent
  context at all." A channel barred from ever informing anything cannot satisfy a
  PASS condition defined by permission to actuate.

*What this does not establish.* It refutes "same concern **because**
outside-the-loop." It does not prove the concerns are different in every sense.
Someone could hold "same" on a different justification; none has been offered.

*A procedural defect in how the first answer was obtained.* The options the
operator chose between were written by this document's author, who benefits from
"same concern." "Same" was presented first and closed with "everything else in
the doc applies as written," while both alternatives were described purely as
loss and neither was described in terms of what it gains. That framing was
leading, and a document whose pitch is pre-registration discipline should not
have had a declaration recorded that way.

*Consequence.* This document has **no claim on the reopening clause**, which
stays empty through 2026-12-01. §1's timing argument loses its force with it: it
was justified as pre-declaring a *reopening* premise, and R6 independently shows
the blindness it claimed was not available. §3's frozen interlocks — "different
dependent variables on different populations," "an adjacent question" — were
right, and the "same concern" answer was the thing that contradicted them.

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

**T2. Task difficulty confounds the whole profile.** A long session with many
turns and many corrections may reflect a hard problem rather than anything about
the agent's prior state. Difficulty is not measured anywhere in this design and
is not randomly assigned: agents are given the work that arrives, and whatever
selects which agent gets which task is exactly the sort of thing prior state
could correlate with. A model that appears to predict effort from prior state
may be recovering the task-assignment process instead.

This is not fixable by a bigger sample — more sessions add more of the same
confound. It needs either a difficulty covariate that is itself observable and
not judge-derived, or a within-agent design that holds the work roughly fixed.
Neither exists in this document, and a Phase 2 registration that does not
supply one is measuring something it cannot name.

**T3. Axiom-2 / anti-RLHF, and the subtler reactivity case.** A speed metric
wired to a verdict hard-codes optimize-toward-fast, the same defect the
tournament identified in the recent-failure-rate gate
(punish-toward-zero-failures). The mitigation there is structural, not
procedural: eval-only, never read by the verdict path.

The subtler case is not wiring but *visibility*. Governance output is surfaced
into agent context (see T4). If an effort profile were ever surfaced the same
way, the measured agents could see the quantity they are scored on, and turn
count is trivially suppressible — end sooner, ask fewer questions, batch less.
The metric would then be reactive, and reactivity of a measure that rewards
brevity pushes directly against the productive friction in T1. So the constraint
is stronger than eval-only: **an effort profile must never enter agent context at
all**, not as telemetry, not as a dashboard the agent can read, not in a mirror
signal. That is a requirement on the surfacing layer, not on the analysis.

**T4. Loop contamination through the transcript — Invariant 4.** This is the
sharpest repo-specific hazard and it is easy to miss. Governance output is
surfaced *into agent context* by the envelope middleware, so a raw session
transcript contains the loop's own verdicts, metrics, and state commentary. A
judge reading that transcript would be estimating effort partly from the
governance loop's own output, and a signal derived from the loop cannot anchor
the loop. **Redaction of all governance-envelope content from judge input is a
correctness requirement, not a hygiene nicety**, and the redaction must be
verified by a test that fails on leakage rather than asserted in a docstring.

**T5. Judge wording sensitivity.** Anthropic's own account of the pilot names
this as the failure mode they could not fully solve: the tool "is sensitive to a
question's wording; a poorly phrased one can place conversations into categories
that misrepresent them," and — their words — "because no one can read the
underlying conversations, these errors are hard to catch." Their workaround,
piloting questions on a public corpus, failed in the predictable direction:
prompts that behaved on WildChat produced misleading categories on real traffic.
This repo's version of that lesson is already written down as the four states a
zero cannot distinguish. Here the mitigation is available and theirs was not:
the transcripts *can* be read, by the operator, on a sample. That is Phase 1.

**T6. Corpus heterogeneity.** The transcript corpus is harness-local and
per-harness heterogeneous (see Phase 1). A judge prompt calibrated on one
harness's format is not validated for another, and the substrate-plurality
posture this repo already holds elsewhere applies: the harness is a property of
the reading, not a nuisance parameter to average over. Phase 1 results are
reported per harness or not at all.

**T7. Selection.** Sessions that produce transcripts are not a random sample of
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
(`core.dialectic_messages`; `src/dialectic_protocol.py` holds only the in-memory list), which is a record of a review conversation, not of
an agent working. Session transcripts are **harness-local files** — the Claude
Code project store, Codex's own store — which this repo does not own and whose
formats differ per harness. `t_actual` comes from transcript message timestamps
for the same reason: `audit.tool_usage` spans only governance calls, so it is a
lower bound on working time, it covers only agents that call governance tools,
and — before 2026-07-31T19:24:49Z — it recorded no MCP-protocol calls at all. That boundary is `TOOL_USAGE_MCP_INSTRUMENTED_SINCE` in `src/audit_db.py`; #1424 is the fix that closed it, not the defect. An earlier revision cited the remedy as the disease (R8).

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
| D7 | Whether an effort profile addresses the same concern as the registered question, or is a separate track | — | **ANSWERED 2026-08-26: separate track.** Reversed from an earlier same-day answer of "same concern"; both answers, the verifiable basis for the reversal, and the procedural defect in the first are in §5 |

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
  automation fired two scheduled jobs 103 times between them, completing 85 runs against data a human would have queried once. An earlier revision said "93 times," a number formed by adding one job's fired count to that same job's completed count (R8).
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
mode in T5 is Anthropic's own account of running the pilot. The earlier METR
randomized trial cited in §5a — measured 19% slowdown against a self-reported
20% speedup among experienced open-source developers — is the published result
that removed the operator-oracle design; it is load-bearing for that removal and
for nothing else. None of these are
peer-reviewed results and none are load-bearing for anything asserted here; they
are the source of a design, not evidence for it.

An adversarial critique of §1, §4, and §5 was run through `consult` on the local
`gemma4` backend (consultation `d2e7134e`, advisory and not on-record; the
`thorough` lane is unreachable under `privacy="local"`, so it ran degraded to
standard). Two of its findings are incorporated: the task-difficulty confound
now in T2, and the reactivity case now in T3. Its framing of the
different-question problem prompted D7. The rest of its output did not survive
inspection and is not reflected here. One degraded local pass by a single model
is not the adversarial design review this document still lacks.

Repo antecedents: the binding-constraint diagnosis and the de-scoped
adjudication dial are from `eisv-grounding-next-move-v0.md`; the anchor tiering
and Invariant 4 are `src/grounding/outcome_anchors.py`; the supply census this
channel's Phase 0 would extend is `scripts/analysis/eisv_latent_label_supply.py`;
the local-model backend precedent is `src/verification_backend.py`.

## 14. Pre-committed degeneracy check — REFUTED, retained as a worked example

**This check is refuted and holds no authority. See R3, R4, and R5.** Its
thresholds could not fire on any real corpus; the quantity it granted kill
authority to is 55% harness-injected wakes; it applies an undeclared selection
rule that biases away from its own kill condition; and its marker set scored zero
precision and zero recall on the only corpus available.

It is kept rather than deleted because it is the most useful artifact this
document produced: a worked example of a pre-registered kill criterion that was
designed to pass, presented in good faith as the answer to an unfalsifiability
objection, and caught only by independent review. The section below is the
original text, unedited, so the failure is legible.

## 14a. The original section, as committed — threshold fixed 2026-08-26, before any data

This section was written and committed **before any transcript corpus was
measured**, from a container holding exactly one transcript file — this
document's own authoring session. The author could not have seen the
distribution when choosing these numbers. That is the point of writing them
here rather than reporting them afterwards as "the method."

**These thresholds are the author's proposal, not the operator's decision.**
Per the standing rule that a deciding standard belongs to the operator, any of
them may be overridden before the check runs. What may not happen is choosing
them after seeing the output.

### What it asks

Whether the effort profile has any variance to model at all. This is upstream of
every other question in this document: if sessions do not differ from each other
on these quantities, no judge, no EISV model, and no answer to D7 can rescue the
channel.

### The rule-free core

`turns(s)` and `t_actual(s)` require no marker set, no model, and no judgement —
message counts and timestamps. They are the non-absorbable part of this check.

- **Corpus floor.** ≥ 30 sessions and ≥ 3 distinct agent identities. Below
  either, the verdict is `UNDERPOWERED` and no degeneracy claim may be made in
  any direction.
- **KILL if** median `turns` ≤ 2, or the interquartile range of `turns` is 0.
- **KILL if** the coefficient of variation of `t_actual` < 0.25. This is a
  degeneracy floor, not a power criterion: real session lengths vary by more
  than this, and failing it means the corpus is uniform rather than merely
  noisy.
- **Between-agent.** If fewer than 3 agents have ≥ 10 sessions each, the report
  says `SINGLE-AGENT` and makes no claim about between-agent spread. It does not
  substitute within-agent variance for it.

A KILL here closes the channel. Not "defers pending tooling", not "reopens under
a new premise" — closes it, on the same terms the stop rule uses.

### The rule-dependent part, and why it cannot kill on its own

`cycles(s)` needs a marker set, and the marker set is itself a judgement (T5).
So it is reported but is **not** granted kill authority:

- If ≥ 90% of sessions show zero cycles **while `turns` and `t_actual` do vary**,
  the finding is `MARKER-SET-BLIND` — a statement about the instrument, not about
  the world. It licenses revising the markers. It does not license a conclusion
  about effort.
- If `cycles` varies, that validates nothing about whether the markers capture
  re-direction. Reliability is not validity, here as in §5a.

The marker set is frozen at this commit, in the script, so a later revision is
visible as a diff rather than as a silent retune.

### What a PASS licenses

Only that the dependent variable is not degenerate. It says nothing about D7,
nothing about the difficulty confound in T2, and nothing about whether prior
state predicts anything. A PASS is permission to keep asking, not evidence for
any answer.
