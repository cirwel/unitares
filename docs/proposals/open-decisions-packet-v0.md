# Open decisions packet — seven operator calls, packaged

**Status:** decision packet, raised 2026-09-09. **This document decides
nothing.** Every one of the seven items below is the operator's call and is
left open. Nothing here is written into the repo as settled, no threshold is
adjusted, no registered instrument is touched, and no recommendation in this
file is a decision. Where an item says SETTLED, that word attaches to a
*verified fact*, never to the operator's disposition of it.
**Shape:** follows the contract in `docs/proposals/operator-decision-packet-v0.md`
— options, recommendation, reversibility and blast radius, default-if-silent.
Worked precedent for the same form:
`docs/proposals/outcome-fixture-conflation-decision-packet-v0.md`.
**Raised by:** a Claude Code session, branch `claude/operator-decision-packet-9ddfuq`.
**Evidence provenance:** every option set below is derived from files and
commands in this repository, read directly. No option was synthesised from a
model's output, and no advisory mechanism was consulted while writing it.
**Recommendations:** each is labelled non-binding. Their *inputs* are sourced
from the evidence cited beside them. Their *ranking* — whether three edits are
proportionate where one would do, whether a gate should block or advise — is
appetite, and is the operator's.

---

## Why this packet exists

Three mechanisms in this repo look like they could absorb a load-bearing call.
None of them can, and each reason is verifiable in the tree rather than
inferred.

1. **`consult` has no repository access and confers nothing by construction.**
   `ConsultParams` (`src/mcp_handlers/schemas/core.py`) is declared
   `extra="forbid"` and accepts six advisory fields — `brief`, `purpose`,
   `effort`, `privacy`, `allow_degraded`, `response_mode` — on top of the three
   identity fields it inherits from `AgentIdentityMixin` (`continuity_token`,
   `client_session_id`, `agent_id`). There is no file path, no repository
   handle, no diff. Its entire input is a text brief the
   caller composes; it can only see what the caller already knows. On the
   authority side, `docs/proposals/consult-advisory-facade-v1.md` states the
   contract in one line: `consult` "returns advisory model evidence and never
   creates a governance record," and `critique` "remains model advice, not a
   governed peer-review verdict."

2. **A dialectic can only be authored by an agent about its own recovery.** The
   session-creation path in `src/mcp_handlers/dialectic/handlers.py` constructs
   `DialecticSession(paused_agent_id=agent_uuid, ...)` — the requesting agent
   occupies the paused-agent slot by construction — and attaches
   `paused_agent_state`, a capture of that agent's own governance evidence at
   session open. The resolution vocabulary in `src/dialectic_protocol.py` is
   `RESUME` / `BLOCK` / `ESCALATE` / `COOLDOWN`, all of them dispositions of
   that agent. Only `RESUME` is ever instantiated;
   `docs/proposals/operator-decision-packet-v0.md` finding 1 recorded `ESCALATE`
   as a dead enum, and the quorum path it names is separately retired. A
   dialectic therefore has no shape in which a third party's question about the
   repository can be posed, let alone answered. Corroborated by the newest code
   on the subject: `scripts/ops/dialectic_unresolved.py`, merged to `master` on
   2026-09-09 in PR #2153, describes the mechanism as a reviewer objecting to
   "the requesting agent," with a self-clear guard that "correctly refuses
   anyone answering in its place." The requesting agent is the subject
   throughout.

3. **The repo has no mechanism for recording a delegation.** `git grep` for
   `lineage_disclosure` across the tree returns two Markdown files
   (`docs/proposals/operator-decision-packet-v0.md`,
   `docs/proposals/track-b-operator-delegate-design.md`) and **zero** Python and
   **zero** SQL files. The one architecture decision on the subject,
   `docs/proposals/ADR-001-operator-vision-delegation.md`, carries the status
   line "Accepted (decision: do not enable as proposed; pursue Track A + Track
   B)."

So the operator decides. This packet's job is to make each call cheap, not to
make it. Per `docs/proposals/operator-decision-packet-v0.md`: "The bottleneck
for *this* class is packaging, not authority."

## The order, and why

The items are **not** presented in their source numbering. They are ordered by
what serves the project's future, on a steer the maintainer gave this packet
directly in the session that commissioned it — long-term future first, then
adopter-facing correctness over operator convenience, then reducing the
archaeology cost paid per call.

**That steer is a conversational instruction to this packet, not a
repository-written standard.** A grep for "adopter-facing" across `docs/`,
`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` and `docs/dev/DRIFT_LEDGER.md`
returns nothing; the nearest written norm is the residentless-install rule in
`CLAUDE.md`, a different context. Earlier drafts of three of these entries
leaned on "the maintainer's steer toward adopter-facing correctness" as though
it were an established criterion. It is not; it is attributed here once, and
used only to order the list.

The order:

1. **Items that decay on a deadline** — three calls bound to the registered
   2026-12-01 read (**D4**, **D6**, **D5**). Their cheap form exists only until
   the read executes; afterwards every version of each is a post-hoc analysis
   change. Within the group they are ranked by what silence costs on the branch
   where it matters.
2. **Adopter-facing correctness** — three calls about what the shipped artifact
   does and what an adopter is permitted to rely on (**D3**, **D2**, **D1**).
   None decays on a clock; each is a defect class that keeps drawing.
3. **Internal hygiene** — one call about the documentation register (**D7**).
   It reaches an adopter only through one sub-item, and whether that sub-item
   outranks the rest is itself the first question in the entry.

## Reading the entries

Each entry states its class, reversibility and blast radius; the fork; what is
established; mutually exclusive options; a non-binding recommendation with its
cost named; reversibility; blast radius; default-if-silent; and two closing
blocks that exist because two verification passes ran over every entry:

- **Riders** — work that is severable from the choice and attaches to any
  branch. Bundling a rider into one option inflates that option in a comparison
  the operator is being asked to read as like-for-like, so riders are listed
  apart.
- **Corrections and flags carried** — numbers a verification pass corrected,
  claims it refuted, and judgements it caught being stated as findings. Nothing
  a verifier raised is dropped: it is either repaired in the text above it or
  recorded here.

---

# Group 1 — decays on the 2026-12-01 deadline

## 1. D4 — What PASS condition 4's "same family" means

**Class:** authority (what a pre-registered instrument counts as satisfied).
**Reversibility:** reversible until 2026-12-01, one-way after.
**Blast radius:** fleet, and partly external.
**Decays:** yes — at the moment the read executes.

### The fork

`docs/proposals/eisv-outcome-grounding-stop-rule-v0.md` PASS condition 4 reads:
"the winning candidate is the same family as at this read's other lead slice —
an argmax that changes with a nuisance parameter is noise-mining." Nothing in
the repository maps "family" onto the candidate set. The fork is what the phrase
means before the read runs — and, separately, what an unevaluable condition 4
does to the verdict if it is left undefined.

### Established

- **The comparison is exactly one pair of strings.** The registered command
  passes no `--group-by-harness-lane` and no `--telemetry-strata`, so
  `build_matrix_from_db` in `scripts/analysis/eisv_ablation_matrix.py` emits one
  row per scope×window×lead — two rows, task/365/lead 0 and task/365/lead 30 —
  each with one winner cell. "This read's other lead slice" is unambiguous.
- **The printed column is headed `Best EISV/prior model`**, not "Best
  candidate"; the frozen 2026-08-09 table used the header `Selected candidate`.
  Any definition written for a December reader must name the column that will
  actually be on the page.
- **The winner is not a pure argmax.** Selection is lexicographic over
  `(beats_baseline, auc_delta, brier_improvement)` — boolean first.
- **The candidate set is seven names** (`EISV_PRIOR_STATE_MODELS`), and "family"
  is mapped onto none of them anywhere: no code, no test, no doc groups them.
- **The sibling pre-registration reads "family" as the whole candidate set.**
  `docs/proposals/independent-operator-cohort-preregistration-v0.md`: "The
  candidate family is the harness's registered candidate set on that one slice,"
  adopted "verbatim" from the stop rule. Under that reading the compared object
  is the same seven-name tuple at both leads, so condition 4 cannot fail on the
  argmax flip its own rationale names.
- **The registering PR glossed it the other way.** PR #1425 (merged
  2026-07-31 — the PR that registered the stop rule) described "four pass
  conditions fixed in advance: selective p ≤ 0.05, observed delta above `Null
  max p95`, ≥ 150 bad clusters, and **a stable argmax across leads**." That
  phrase never reached the merged document, which says "same family."
- **On every lead-0/lead-30 winner pair recorded in the repo (5 of 5), the
  winner differs by name and by feature axis.** Classified: name-identity 0/5
  met; feature-axis 0/5 met; whole-candidate-set 5/5 met; "pure prior-state vs.
  combined-with-`previous_bad`" 2/5 met. Caveat: none is the registered cohort,
  and two rows predate the trusted-anchor default. This shows the argmax moves
  with the lead parameter; it forecasts nothing about December.
- **Condition 4 binds only where conditions 1–3 all pass**, and there are only
  three independent conditions, because condition 1 implies condition 2 (see
  entry 3).
- **The candidate tuple is not pinned.** `REGISTERED_READ_MANIFEST` pins the
  fixture rule; it does not pin `EISV_PRIOR_STATE_MODELS` or
  `DISPERSION_FEATURE`, and the registered command runs "from a checkout of
  `master`." A name-level definition survives a tuple change; a mapping table
  does not.
- **Amending the sibling is currently free.**
  `docs/proposals/independent-operator-cohort-enrollments.md` records no
  enrollment. Its lane-P read fires at day 58 of an enrolled window, so for a
  lane-P publication to precede the December posting, phase-1 enrollment and a
  first qualifying check-in would both have to land within roughly nine days of
  today. Arithmetically possible, tight, and worth stating as arithmetic rather
  than as an open hazard.
- **Precedent for a dated pre-read edit exists in both documents.** The stop
  rule carries "Pre-declared sensitivity cohort — declared 2026-09-02," added 24
  days after the frozen winners were public and accepted because it added a
  reported cohort without moving the registered predicate. The sibling carries
  an applied "Amendment v0.1, 2026-09-02, made before any enrollment," and the
  code manifest entry is named `independent-operator-cohort-v0.1`.

### Options

| # | Option | What it does | What it costs |
|---|---|---|---|
| **A** | **Winner-name identity.** A dated pre-read clarification: condition 4 is met iff the `Best EISV/prior model` string at task/365/lead 0 equals the one at lead 30. | Matches PR #1425's contemporaneous gloss; needs no mapping table; immune to a change in the candidate tuple. The feature-axis variant gives the same verdict on all five recorded pairs, so the distinction is presently immaterial. | The strictest live reading. Unmet on 5/5 recorded pairs, so on the one branch where condition 4 binds this is close to pre-committing to FAIL. It is written with the frozen winners public, so the amendment must say so; it cannot be presented as a pre-evidence choice. |
| **B** | **Adopt the sibling's whole-candidate-set reading.** Condition 4 is met iff the same candidates were fitted at both leads. | Removes the contradiction between two pre-registrations in favour of the one already published and code-bound, and is readable from the printed table. | It cannot fail on an argmax change — the condition's own stated rationale. With condition 1 implying condition 2, "four PASS conditions" become two live ones plus a coverage check. `CLAUDE.md`'s stop-rule exemption says "Do not weaken, re-run, or 'refresh' them"; converting a condition that can fail into one that effectively cannot is the clearest form of weakening, done after the frozen table showed the argmax moving. |
| **C** | **A coarser partition: pure prior-state vs. combined-with-`previous_bad`.** Met iff both winners fall on the same side. | The only partition in the space that changes a recorded outcome (2 of 5 instead of 0 of 5). | That is exactly what makes it the most exposed: it is indistinguishable, to a later reader, from choosing the standard that yields the wanted verdict. It has no independent rationale — nothing in code, tests or docs treats that as a boundary, and the combined models share their EISV feature with their pure counterparts, so the cut runs across the axes rather than along them. |
| **D** | **A tolerance reading.** Met iff the two leads' winning deltas are not distinguishable — the printed `AUC delta 95% CI` at each lead covers the other's point estimate. | Applicable from the registered output alone (the CI is a printed column, fed by `best_auc_delta_ci`). Not vacuous: it fails on a flip between well-separated candidates. Tracks "an argmax that changes with a nuisance parameter is noise-mining" more closely than a bare string match. | It is an interpretation of "stable," not of "family" — legitimate only once intent is the interpretive key, which is also what option A relies on. The strongest form wants per-candidate deltas, which the matrix does not print, so it is available only in the weaker CI-overlap form or by adding a reported column before the read. |
| **E** | **Leave "family" undefined; pre-declare what that does to the verdict.** A dated clause: condition 4 is unevaluable as registered, the read publishes both winner names verbatim, and an unevaluable condition 4 bars a PASS — the report may say "conditions 1–3 met, condition 4 unevaluable," with the scientific inference capped as the smallest-relevant-effect clause already caps it. | The move the document already makes for its smallest-relevant-effect slot ("If that effect remains unspecified, report the scientific inference as `INCONCLUSIVE`"). Converts an ambiguity into a determinate conservative rule without inventing a definition. | It does not answer the question. On the PASS-leaning branch the practical outcome is A's without A's stated reason, and strictly harsher — A at least can be met. If the operator's intent is that a stable-argmax requirement should be *satisfiable*, this forecloses that silently. The permissive variant ("conditions 1–3 decide the operational verdict") costs the same to draft and meets the same weakening objection as B. |

### Recommendation (non-binding)

**A, in the winner-name form.** Reasoning, and the line between what is applied
and what is chosen: the *classification* is criterion application — PR #1425's
gloss is contemporaneous, pre-data, and says "stable argmax," and `CLAUDE.md`'s
"Do not weaken, re-run, or 'refresh' them" is a standard the operator wrote
down, which B fails on its face. The *ranking* of A over D and E is not. Whether
a stable-argmax requirement is worth a condition that would have been unmet 5/5
is a judgement about how much the requirement is worth, and it is the
operator's.

Named cost, so this is not sold cheap: if conditions 1–3 pass, A is the
condition most likely to produce the FAIL — closure of the scheduled read track,
though not of EISV; the stop rule's "What continues regardless" section governs
that. If the operator wants the requirement to remain satisfiable, **D** is the
branch that keeps it capable of being met without going vacuous. If the operator
will not touch the registered text at all, **E** is the fallback with the same
disclosure cost and less definitional commitment — but it is strictly harsher
than A, not safer.

### Reversibility

Reversible until 2026-12-01; one-way after. Before the read, any of A–E is a
text edit revisable by a second dated amendment, and no option forecloses
another. At the moment the read executes, the option set collapses to one: the
read prints two model names, and any definition supplied afterwards is a
standard chosen with the data in hand. The document's own instruction, in the
pre-registered gate section: "Do not adjust these thresholds after seeing the
read. The point of writing them down now is that they were chosen before the
data existed." Nothing here touches the reopening clause.

### Blast radius

(1) The 2026-12-01 verdict and whether Stage B may be reconsidered. (2) The
independent-operator cohort pre-registration, which adopts this condition
"verbatim," is bound into code via `REGISTERED_READ_MANIFEST`, and whose lane-P
sentence is published for an external audience — zero enrollments today, so
amending it is free now and will not be after a phase-1 entry lands. (3) Every
future citation of the December result: "all four PASS conditions were met" is
the sentence that gets quoted, and today one of the four has no meaning and
another is implied by a third. (4) The README claim table is untouched by every
option; the frozen row does not change.

### Default if silent

The read runs as registered and prints two model names with no rule for
comparing them. **Two readings of what happens next are both live, and this
packet does not pick between them.** The stop rule says PASS "requires all of"
1–4 and FAIL fires "if any condition is unmet," which reads as turning an
undefined condition into a FAIL. The competing reading is that a term with no
content imposes no requirement, so the gate is effectively conditions 1–3 — which
is the de facto effect of the sibling's published and code-bound reading. "Unmet"
and "unevaluable" are not the same word, and which one an undefined condition is
*is* the question. Option E exists precisely because the text does not settle it.

Either way, the only move available to the December report's author is to supply
a definition after seeing the two names, which is the failure `CLAUDE.md` names:
"State a deciding standard as a choice before applying it, not afterwards as
'the method.'" Silence costs nothing if conditions 1–3 fail and is expensive if
they pass. Separately, because nothing pins `EISV_PRIOR_STATE_MODELS` or
`DISPERSION_FEATURE`, silence also leaves the argmax's domain free to move on
`master` between now and the read.

### Riders (orthogonal to the choice)

Both attach to any option, including E, and neither should be read as a reason
to prefer one branch:

- **Pin the candidate set.** Add `EISV_PRIOR_STATE_MODELS` and
  `DISPERSION_FEATURE` to `REGISTERED_READ_MANIFEST`. This answers a defect the
  manifest has independent of how condition 4 is read.
- **Repair the sibling's "adopted verbatim" sentence** so it is true of whatever
  reading is chosen. It removes the cross-document contradiction under any
  branch.

### Corrections and flags carried

- **Corrected:** the printed header is `Best EISV/prior model`, not "Best
  candidate" — a fabricated column name in an earlier draft, load-bearing
  because option A is applied off the printed table. Corrected: the row builder
  is `build_matrix_from_db` / `build_matrix_row`, not "build_matrix_rows." The
  behavioural claim (two rows) reproduced.
- **Corrected:** the sibling's Amendment log line "(none — v0 as registered)" is
  stale — an applied v0.1 amendment sits in its body. This strengthens rather
  than weakens the finding that dated pre-read amendment has precedent there.
- **Corrected:** the 2026-09-02 sensitivity-cohort block was added 24 days after
  the frozen table, not "months." Corrected: the "do not adjust these
  thresholds" line sits in the pre-registered gate section, not at the document's
  close. Corrected: grep counts for "family" and "condition 4" were understated;
  in particular the sibling has its **own** condition 4, which is the most
  confusable hit and was missed.
- **Flag carried, unresolved:** reading the sibling's "family" as a
  fitted-coverage parity check is a charitable gloss its own text does not
  support — it files the fitted count as a separately reported quantity. Under
  its literal words, condition 4 is strictly vacuous, which strengthens the case
  against B.
- **Flag carried:** the 2026-09-02 precedent is weaker support for A than it
  first appears — that block's own words are "without authority" and "The
  registered predicate decides the four PASS conditions," whereas A settles the
  predicate.
- **Resolved:** option D (tolerance) was absent from an earlier option set,
  which made the fork read as a trilemma between strict-and-fails, vacuous, and
  gerrymandered. It is added above. Resolved: the riders were bundled into option
  A and then used to fault option E; they are severed above.

---

## 2. D6 — An agent-stratified selective null before the read

**Class:** authority (what the registered instrument is).
**Reversibility:** reversible before the read; the cheap form expires at it.
**Blast radius:** narrow in code, wider in claims.
**Decays:** yes.

### The fork

Should the selection-aware null be changed to permute EISV readings only
*within* an agent — and if so, as the deciding instrument, or as a declared,
non-authoritative second column reported beside the registered result?

### Established

- **The registered null permutes across agents by construction.**
  `estimate_selective_null` builds one block per `(agent, prior-state snapshot)`
  cluster and does a single agent-blind shuffle over all cluster keys.
  `PRIOR_STATE_FIELDS` contains no `agent_id`, no timestamp and no label, so
  after permutation prior state carries no agent information.
- **The scorer splits by time, not by agent.** `build_model_scores` →
  `split_by_time` sorts by timestamp and cuts at `train_fraction`. There is no
  `GroupKFold`, no `groups=`, no stratification anywhere in
  `scripts/analysis/eisv_ablation_matrix.py`. The same agent therefore sits in
  train and test.
- **The confound is not theoretical — it reproduces end-to-end on the real
  harness.** A verification pass built a synthetic cohort with prior state
  constant within each agent and agent base rate equal to agent-level prior
  risk — zero within-agent predictive content, pure between-agent heterogeneity
  — and ran it through the real `build_matrix_row` at 200 resamples. In 5 of 5
  trials the registered agent-blind null returned a signal candidate: best AUC
  delta +0.076 to +0.238 against a null-max p95 of 0.045–0.068, selective p
  0.005–0.015. **Both PASS conditions 1 and 2 cleared on a cohort with no
  within-agent signal whatsoever.** This is an existence proof under a maximal
  construction, not a magnitude estimate for the real cohort. Its necessary
  condition matters: when the same construction placed each agent's rows
  contiguously in time, `split_by_time` accidentally separated agents into
  train and test and the effect vanished entirely. The confound bites exactly
  when the same agent sits in both — so the two mechanism facts are one coupled
  fact, not two.
- **The null cannot touch the condition most likely to bind.**
  `count_bad_clusters` is a separate set-cardinality pass; `estimate_selective_null`
  is not in its path. Condition 3 (`Bad clusters ≥ 150`) is therefore untouched
  by every option here. Frozen cohort: 28–29 bad clusters, 16 agents.
- **A closer written criterion than the CV precedent exists, and it is dated
  and on-point.** `docs/ontology/falsification-design-system-audit-2026-08-23.md`
  — whose own status line names the outcome-grounding stop rule — carries a
  six-check table whose **Unit** check asks: "Are uncertainty and resampling
  based on independent agents or episodes rather than repeated rows, shared
  hosts, or **shared state snapshots**?" The registered null resamples on shared
  state snapshots.
- **The v7-fhat precedent is real but never executed.**
  `docs/ontology/v7-fhat-spec.md` §6.3 pre-registered "5-fold CV **grouped by
  agent**" for the same target, and the v3 draft was rejected in 2026-04 because
  three of five targets had too few active agents for grouped CV — the project
  re-scoped rather than dropping the standard. But Session 2 never ran (SC2
  tripped at r=0.9949; `docs/ontology/plan.md` row S12 records the same). Treat
  §6.3 as a recorded methodological preference, not a precedent in force. Grouped
  CV and a within-agent null are also different instruments that happen to point
  the same way.
- **Feasibility is unknowable today.** `estimate_selective_null` refuses below
  three permutable units; a stratified null needs that per stratum. The deciding
  number is total `Null clusters` per agent, which the frozen transcription
  dropped and which `docs/operations/falsifiability-power-audit-2026-08-23.md`
  lists as unrecoverable without a live re-run the same audit says is not
  justified.
- **A dated precedent went the other way on this same instrument.** The power
  audit found the baseline anti-predictive (frozen baseline AUC 0.427–0.435),
  noted "a below-chance reference can change both observed deltas and the
  max-over-candidates null," called it "also a power sink" — and filed it under
  "Not fixed here," deferring to prospective testing.
- **Silence does not freeze the instrument.** `record_read_receipt` writes no
  commit, hash or code version, and the registered command runs "from a checkout
  of `master`." The seed is likewise unpinned (`--uncertainty-seed` default 0),
  while the later `independent-operator-cohort-v0.1` entry in the same manifest
  sets `binds_uncertainty_seed=True`.
- **Code blast radius is outside shipped runtime.** `estimate_selective_null` is
  referenced only in `scripts/analysis/eisv_ablation_matrix.py` and its test
  file; `scripts/analysis/ablation_power_probe.py` reaches it indirectly through
  `build_matrix_row`. Nothing in `src/`, `governance_core/`, the server or the
  dashboard. `scripts/analysis/legacy_coherence_dependency_shadow.py` carries its
  own cluster helper and does not call it.

### Options

| # | Option | What it does | What it costs |
|---|---|---|---|
| **A** | **Change nothing; name the confound as a limitation in the December report.** | The registered null decides conditions 1 and 2 as written; the report adds the unit-of-resampling limitation to the disclosures it already owes, citing the 2026-08-23 Unit check by name. Exactly the anti-predictive-baseline precedent. | Asymmetric. Costs nothing on a FAIL — the Unit check already caps a non-detection below `REFUTED`, and the null cannot reach condition 3. On a PASS there is no guard at all, and the existence proof above shows the missing guard is demonstrably missing, not merely theoretically. |
| **B** | **Declare a within-agent null now as a second, non-authoritative column.** | A within-agent permutation mode behind a flag, plus a dated declaration in the stop rule: the registered agent-blind null decides all four PASS conditions; the stratified column is computed at the read from the same cohort and reported beside it without authority. | Real code: a mode in `estimate_selective_null`, a per-stratum minimum-unit rule, a CLI flag, updated reading instructions, and four tests that call the estimator (plus two on the qualifier path). A permanent declaration that cannot be un-declared. And the column may come out degenerate: agents contributing one or two clusters can only permute to themselves, pushing the stratified p toward 1.0 — and whether the December cohort has the geometry cannot be known today. |
| **C** | **Amend the registered read so the within-agent null decides.** | A PR against `REGISTERED_READ_MANIFEST` and the stop rule, disclosed as a protocol deviation. | One-way door and the most expensive branch. A decision-rule change by an analyst who has already seen the frozen slice, so the design-system audit's own **Decision rule** check ("fixed before the read") fails and the amendment precedent ("before any enrollment") is unavailable. Because it makes PASS strictly harder it reads as steering toward the kill criterion — the mirror image of p-hacking, not a defence against it. Forecloses A and B. |
| **D** | **Fix the reference instead of the permutation unit.** | Give the baseline the agent information it lacks — `_fit_group_rates` builds it from a one-line key function, `score_deltas_vs_baseline` already takes `baseline_name`, and `agent_id` is on the row and outside `PRIOR_STATE_FIELDS`, so it survives permutation intact. Report an agent-aware reference as a non-authoritative second column. | Addresses the other half of the same diagnosed defect and is cheaper than B — no per-stratum minimum-unit rule, and it does not have B's degeneracy blocker. But it is the less-examined lever: no written criterion in the repo points at it the way the Unit check points at the permutation unit, and the anti-predictive-baseline finding was itself deferred rather than acted on. |
| **E** | **Characterise it synthetically first, decide later.** | Use the database-free `scripts/analysis/ablation_power_probe.py` path to establish formability and seed sensitivity at the frozen geometry, with zero live-data access. | Spends the scarce resource, which is the pre-read window. And it cannot answer what matters: `synthesize_cohort` draws its latent i.i.d. per cluster and assigns agents round-robin — zero agent effect by construction — so without a new agent random effect it will show the stratified and global nulls behaving alike. The deadline does not move while it runs. |
| **F** | **Run as registered; if the read PASSes, compute the within-agent null as a disclosed post-hoc robustness check before publishing.** | Costs no pre-read window, no code today, and no permanent declaration, while answering the exact asymmetry that argues against A. Nothing in the three governing documents forbids it: the design-system audit's Protocol check requires analysis changes be "prevented or fully disclosed," and the stop rule's binding sentence is "Do not adjust these **thresholds** after seeing the read," which a column with no authority does not do. | It is post-hoc, and a reader may discount it for that reason no matter how it is disclosed — the whole point of pre-declaring is that pre-declaration is credible in a way disclosure is not. It also depends on the December author remembering to do it, with nothing written down today that obliges them. |

### Recommendation (non-binding)

**B.** The *inputs* are sourced: the confound reproduces end-to-end, the Unit
check is a dated criterion in a document that names this instrument, and B costs
the registration nothing because the registered null still decides all four
conditions. The *ranking* — that a permanent declaration and the code are worth
buying now rather than relying on F, or on A's limitation note — is appetite,
and it is the operator's.

If the operator does not want to spend the code, **A is genuinely defensible**
on the project's own precedent: the same audit found a defect in the same
instrument and deliberately left it for prospective testing, and A costs nothing
on a FAIL. **F** is the honest middle: it buys most of B's protection at
zero pre-read cost and pays for it in credibility. What I would *not* recommend
is **C**: a stricter null chosen after seeing the frozen slice's smallest
selective p is an analyst choice steering toward closure, and steering toward
the conservative answer is not a defence.

### Reversibility

A is reversible until 2026-12-01 and irreversible after. B's code is reversible;
its declaration is not — a pre-declared column cannot be un-declared. C is a
one-way door on the fixture packet's own stated rule, and would be a further
disclosed deviation on this registration. D's column carries the same
declaration cost as B if pre-declared, and none if taken as F's shape. E
forecloses nothing but consumes the window. Foreclosure: A, B, D, E and F are
mutually compatible before the read; C forecloses the rest, because once the
deciding instrument moves a non-authoritative column is moot. After 2026-12-01,
B, C, D and E lose their cheap form; F and a fresh registration survive, the
latter at higher cost and with the reopening clause attached.

### Blast radius

Code: `estimate_selective_null` and its two consumers, both under
`scripts/analysis/`, plus six tests in `tests/test_eisv_ablation_matrix.py` (four
call the estimator; two exercise the qualifier path). Nothing in `src/`,
`governance_core/`, the server, the dashboard or any adopter runtime path.
Contracts: the 2026-12-01 read, and only its conditions 1 and 2. People: this
operator now; the December report's readers; and indirectly anyone reading the
README claim table or the paper's claim status. Adopters are not affected at
runtime — this is analysis scripting — so the adopter-facing exposure is entirely
in what the project is later entitled to claim.

### Default if silent

The registered read runs with the agent-blind null and no stratified column.
Three things follow. (1) Silence does not freeze the instrument — the receipt
records no code version and the read runs from `master`, so an unrelated
refactor between now and December lands inside the registered read with nothing
recording it; the same is true of the seed. (2) Silence is nearly free on a FAIL
and not free on a PASS: `count_bad_clusters` is independent of the null so
condition 3 is untouched, and the Unit check already caps a non-detection below
`REFUTED` — but on a PASS there is no symmetric guard, and the confound is
demonstrated. (3) The cheap form decays: a pre-declared, non-authoritative column
that costs the registration nothing exists only until 2026-12-01. Option F
survives the deadline; nothing else cheap does.

### Riders (orthogonal to the choice)

- **Pin the instrument.** Record the code version in `record_read_receipt` and
  pin `--uncertainty-seed` for this manifest entry. This touches
  `estimate_selective_null` not at all, is available under every branch including
  A, and answers a defect that is branch-independent. An earlier draft charged
  this as a cost of choosing A, which tilted the A-versus-B comparison; it is
  severed here.

### Corrections and flags carried

- **Corrected:** the MDL seed-dependence figures cited from the stop rule sit
  inside its **withdrawn** 2026-07-31 section, measured on the contaminated
  all-scope cohort, which the document itself calls "not a transferable bound
  for trusted anchors." The seed-sensitivity *argument* (fewer permutable units
  per stratum → more seed variance) stands on its own; the number does not carry
  the authority an earlier draft gave it.
- **Corrected:** "five locked tests" — four call the estimator, a fifth
  constructs the dataclass directly, and a sixth on the same qualifier path was
  unnamed. "Exactly two consumers" is true only if indirect calls count; the
  probe reaches the estimator through `build_matrix_row`.
- **Corrected:** the in-flight-PR check was reported as indeterminate because
  `gh` is unavailable in this environment. It is determinable through the GitHub
  API and was run for this packet: **zero open PRs** in `CIRWEL/unitares` at time
  of writing. An earlier pass saw #2153 and #2155 open; both have since merged.
  The single-writer check in `CLAUDE.md` is satisfied affirmatively.
- **Flag carried:** "an agent-stratified null is a strengthening (it makes PASS
  harder)" was labelled a confirmed fact in an earlier draft. It is a reasoned
  expectation about a statistic that has never been computed on any cohort — not
  a code property. It is also the claim doing the work: it is why `CLAUDE.md`'s
  "do not weaken, re-run or refresh" is said not to reach the change, and why C
  reads as steering toward closure. Hedged here.
- **Flag carried:** reading the Unit check as *condemning* this null rather than
  *describing* it is an interpretation. The `(agent, prior-state snapshot)` block
  is the code's own remedy for repeated rows — `count_bad_clusters` says so. The
  condemning reading is defensible and probably right, but it is a reading.
- **Corrected:** an earlier draft said option B is "byte-for-byte the shape the
  operator accepted on 2026-09-02." The record says the operator *delegated* the
  selection to a working agent with one criterion, and the agent selected R1.
  Under `CLAUDE.md` that delegation was explicit and recorded, so R1 was a
  legitimate decision — but an agent-selected precedent is weaker support for
  "this shape is already blessed" than an operator-selected one. The honest form
  is: the shape a delegated agent selected seven days ago, which the operator has
  not objected to.
- **Resolved:** options D (agent-aware reference) and F (post-hoc disclosed
  column) were absent. D is the second lever on the packet's own diagnosed
  two-sided defect; F was foreclosed by an assertion that after the read "only a
  fresh registration survives," which none of the three governing documents
  supports. Both are added above.

---

## 3. D5 — Whether PASS condition 2 is redundant

**Class:** not a value call on the fact; taste on the disposition.
**Reversibility:** reversible; the deadline is the one-way element.
**Blast radius:** surface.
**Decays:** yes — the cost lands on 2026-12-01.

### SETTLED — this is a fact, not a choice

**Condition 1 (`selective p ≤ 0.05`) strictly implies condition 2 (`AUC delta >
Null max p95`) at every reachable resample count.** This was established twice
independently, from source, not taken on report:

- Both statistics come from one `estimate_selective_null` call, one null list,
  one seed. `p95 = _percentile(null_best, 0.95)` and
  `selective_p = (at_least_as_extreme + 1) / (len(null_best) + 1)` read the same
  list; the observed statistic in condition 2 is the identical float passed in
  as `observed_best_delta`. No sampling variation can separate them.
- Index sweeps over N = 1…5000 and value-level sweeps over hundreds of thousands
  of constructed null lists found **zero counterexamples**, in both verification
  passes. Condition 1 is unreachable at all for N < 19.
- The result survives five alternative percentile conventions (linear, lower,
  higher, nearest-rank, midpoint), so a future refactor of `_percentile` would
  not by itself break the entailment.
- The implication runs one way only. Condition 2 is strictly weaker: at N = 400,
  k = 20 gives selective p = 0.0524 (condition 1 fails) while observed can still
  exceed p95 (condition 2 passes). The `+1/+1` smoothing makes condition 1
  marginally the stricter of the two.
- At the registered 400 resamples the entailment holds with exactly zero index
  slack: k ≤ 19 puts sorted indices 0…380 strictly below observed, and p95
  interpolates indices 379 and 380. The same zero slack holds at N = 200.
- **Nothing in the repo evaluates the four PASS conditions.** The harness prints
  the columns; a human applies the rule to the printed table. So keeping,
  annotating or striking condition 2 changes no code and no test.

Stated plainly: "selective p ≤ 0.05" and "observed above the null's 95th
percentile" are the textbook one-sided level-0.05 test written two ways.

### The remaining question

Not whether condition 2 is redundant — it is — but **whether to record that fact
before 2026-12-01, and where.**

### What bears on it

- **Condition 2 is *not* the only thing forcing `Null max p95` into the report.**
  The same pre-registered document imposes it twice more, independently: an
  unconditional prohibition — "**Do not quote `AUC delta` without anchor scope,
  cutoff, bad rows, bad permutation blocks, agents, null p95, and selective
  p.**" — and a FAIL reporting clause requiring any published bound be "reported
  with anchor scope, cutoff, bad rows, permutation blocks, agents, selected
  delta, null p95, selective p." An earlier draft asserted exclusivity four
  times and built the entire case against striking condition 2 on it. That case
  does not hold.
- **And the cited incident cuts the other way.** The 2026-08-09 frozen
  transcription dropped the `Null max p95` column, and the errata calls it
  "load-bearing, not decorative." But condition 2 was already in force when that
  happened — the stop rule was registered 2026-07-31 and its 2026-08-17
  correction says it "changes neither the date nor the four PASS conditions."
  Condition 2 was forcing the column, the unconditional prohibition was forcing
  it, and it was dropped anyway. The incident is evidence that condition 2 does
  *not* function as a forcing mechanism, not evidence that it does.
- **The remaining sound objection to striking it is procedural, not
  evidential.** `CLAUDE.md`: "Exempt: pre-registered scientific stop rules …
  Do not weaken, re-run, or 'refresh' them." The document itself: "Do not adjust
  these thresholds after seeing the read." Those are criteria the operator wrote.
- **A rounding hazard sits at exactly the PASS boundary.** `_fmt_float` prints
  `AUC delta` and `Null max p95` at three decimals. When condition 1 passes at
  k = 19 with tied nulls at the interpolation indices, `observed − p95` can be
  under 0.0005: the rendered table shows identical values, and a reader applying
  condition 2 off the markdown would mis-FAIL a slice condition 1 passed.
- **The entailment is a property of the current estimator**, not of the two
  statistics as concepts. It depends on `_percentile`'s convention and on the
  `+1/+1` smoothing. Any note must be scoped to the implementation or it becomes
  a stale claim.
- **The frozen slices are reconstructible without a live read, with the
  denominator corrected.** The frozen 2026-08-09 command passes no
  `--selective-null-resamples`, so it ran at the argparse default of 200, not the
  registered 400. At N = 200, selective p ≥ 0.070 forces observed ≤ p95; the
  binding slice is task/30d/30m at exactly 0.070, forced for all N ≥ 86. All 12
  frozen slices report selective p in 0.070–0.567, so condition 2 failed on all
  12, exactly where condition 1 failed. The successful-resample count is never
  printed (`SelectiveNull.resamples` is not copied onto the row), so the caveat
  is "determined for N ≥ 86 of 200," not unconditional.

### Options

| # | Option | What it does | What it costs |
|---|---|---|---|
| **A** | **Record nothing.** | Zero effort, zero protocol exposure. | The December report names conditions 1 and 2 among the failed conditions with nothing on record saying they are one test. Whether that reads as two independent signal tests failing is a matter of how the report is written (see the flag below); the rounding hazard at the PASS boundary is unmitigated either way. |
| **B** | **Note it at read time, not now.** | Write nothing today; the December report says, when it names failed conditions, that conditions 1 and 2 are one test. | Answers the presentation risk with zero edits to any pre-registration-adjacent file — but depends entirely on the December author holding this fact, with nothing written down to remind them. It does nothing about the rounding hazard. |
| **C** | **Record it outside the registered file** — a dated non-normative note in `docs/operations/` or `docs/EVALUATION_INDEX.md`. | Lowest protocol exposure of the written options; the pre-registered text is untouched. | Puts the note where the December reader is least likely to look — that reader opens the stop rule, not the evaluation index. Needs the implementation-scoping caveat or it rots silently when `_percentile` or the smoothing changes. |
| **D** | **Record it inside the stop-rule document as a dated, explicitly non-normative annotation.** | Follows the precedent already in that file: the condition-3 feasibility diagnostic is itself a post-registration exploratory note that declares in its own text that it changes no cutoff, cohort, threshold, PASS condition, kill criterion or reopening rule. | Highest visibility to the person who applies the rule. The price is an edit to the one file where every edit carries the burden of proving nothing normative moved, and the note must say so in its own text. The precedent is squarely on point but it is a precedent for a *permitted* edit, not a general licence. |
| **E** | **Pin the entailment with a database-free regression test, and record nothing in prose.** | A test asserting that `selective_p ≤ 0.05` implies `observed > p95` for the shipped `_percentile` and p-value formula, so a future change to either surfaces as a failure. No documentation edit, no protocol surface at all. | It records the fact where a maintainer will trip over it and nowhere a December reader will read it. It converts a documentation claim into a maintained contract — the point, but it means a future estimator change must consciously deal with it. |
| **F** | **Widen the printed precision on the two columns.** | A code-only change to `_fmt_float`'s application to `AUC delta` and `Null max p95`, removing the boundary rounding hazard. Zero protocol exposure. | Answers only the rounding hazard, not the redundancy. Touches the analysis script before a registered read runs from `master`, which is exactly the unpinned-instrument problem entry 2 raises — so it should carry a note saying what changed and when. |
| **G** | **Strike condition 2 from the registered rule as redundant.** | The gate loses nothing: every FAIL condition 2 could produce, condition 1 already produces. | Recommend against, and the reason is **procedural**. It edits a pre-registered stop rule, which `CLAUDE.md`'s exemption tells agents not to weaken, and a rule edited before a read cannot later claim it was not edited. The *evidential* argument previously offered for keeping condition 2 — that it uniquely forces `Null max p95` — is false, and the incident cited for it cuts the other way. The procedural ground is the only sound one, and it is sufficient. |

These are not mutually exclusive in the strict sense — E and F are code, the
rest are prose — but the prose options (A–D) are one pick, and E and F are each
an independent yes/no.

### Recommendation (non-binding)

**D + E**, with **F** as a separate yes. Add a dated, explicitly non-normative
annotation to `docs/proposals/eisv-outcome-grounding-stop-rule-v0.md`, in the
declared form the condition-3 feasibility diagnostic already uses, stating
three things: (1) condition 1 entails condition 2 under the shipped estimator,
so the December report should not present them as two independent tests; (2) the
entailment is a property of the current `_percentile` convention and `+1/+1`
smoothing; and (3) that this note changes no condition, threshold, cutoff,
cohort, kill criterion or reopening rule. Pin (2) with the regression test.

Why D over C: the December reader opens the stop rule, and the archaeology this
packet exists to remove reappears in full if the note lives where they will not
look. Named cost: D is an edit to the file where edits are most expensive to
justify, and it is safe only if the note declares its own non-normativity in its
own text.

Note what changed from an earlier draft's version of this recommendation: it
listed as reason (2) that "condition 2's non-redundant job is forcing `Null max
p95` into the report, which is why it stays written." That reason is withdrawn —
it is false. Condition 2 stays written because a pre-registered rule should not
be edited before its read, which is a criterion the operator wrote down.

If the pre-read edit is unacceptable at any price, **C + E** gets most of the
value. **B** is the cheapest honest answer to the presentation risk alone. **A**
is free today and costs on 2026-12-01. **G** should not be taken.

### Reversibility

The fact is settled; there is nothing to undo about it. A–F are fully
reversible: no cohort, cutoff, threshold, condition or decision path moves, and
nothing machine-checks the PASS conditions. G forecloses the others — a
pre-registered rule edited before the read cannot later claim it was not edited.
The genuinely one-way element is the deadline, not any option: once the December
report is published, correcting how it characterised the conditions is a
disclosure event rather than an edit.

### Blast radius

(1) The operator, who applies the four conditions by hand on 2026-12-01 —
nothing in the repo does it for them. (2) Outside readers: `README.md` and
`docs/PRODUCT_DEFINITION.md` were both corrected on 2026-08-23 for overclaiming
this result in the negative direction, and `scripts/diagnostics/check_doc_health.py`
was given lint rules to hold that line. (3) Future contributors to
`scripts/analysis/eisv_ablation_matrix.py` — `_percentile` and the `selective_p`
formula make the entailment true today, and nothing tells a maintainer that
changing either would falsify a recorded claim about a pre-registered rule.
Option E is what closes ring 3.

### Default if silent

Nothing breaks and no code is affected. On 2026-12-01 the read runs as
registered and all four conditions are applied by hand. If the signal conditions
fail — which every one of the 12 frozen slices did, selective p 0.070 to 0.567,
and where condition 2 also failed on all 12 — the report names conditions 1 and
2 among the failed conditions. Whether that reads as two independent signal
tests having failed depends on how the report is written; the operator now holds
the fact, so the careless phrasing is avoidable without any edit today. If the
read passes near the boundary, the three-decimal table can show `AUC delta` and
`Null max p95` as an apparent tie, and a reader applying condition 2 off that
table mis-FAILs a slice condition 1 passed. That one is not avoidable by
knowing the fact; it needs F.

### Corrections and flags carried

- **Corrected:** the exclusivity claim ("condition 2 is the only written
  requirement forcing `Null max p95` into the report") is false and appeared in
  four places. The stop rule's unconditional "Do not quote `AUC delta` without …
  null p95" prohibition and its FAIL reporting clause both force it
  independently. The recommendation above is rebuilt on the procedural ground
  alone.
- **Corrected:** the frozen-slice reconstruction was caveated as "N ≥ 128 of
  400." The frozen read used the argparse default of 200, and the true threshold
  is N ≥ 86 at that denominator. The conclusion (condition 2 failed on all 12)
  survives and is more robust than stated.
- **Corrected:** "both conditions flip at the same permutation" is wrong and
  contradicted the same draft's own counterexample. Condition 1 fails strictly
  earlier.
- **Corrected:** `docs/EVALUATION_INDEX.md` was **not** among the surfaces
  corrected on 2026-08-23 — the audit explicitly places it on the other side
  ("The internal canon was already correct"). `scripts/diagnostics/check_doc_health.py`
  was corrected and was not named.
- **Corrected:** specific trial counts from the brute-force sweeps were quoted as
  findings; they are artifacts of an unstated RNG and did not reproduce exactly
  across passes. The qualitative results reproduced in both. The scratch scripts
  cited in an earlier draft were not present for re-running; every check above
  was rebuilt from source.
- **Flag carried:** "the FAIL report **will** overclaim if nothing is noted" was
  stated as a determinate finding. It is a prediction about a report that has not
  been written and about how an audience will construe it. It is also
  self-undermining: the operator now holds the fact, so the careless phrasing
  requires the December author to write carelessly while in possession of it.
  That single prediction is what turned option A from "zero cost" into "buys the
  outcome the audit exists to prevent." Hedged above; how much presentation risk
  to buy down at what pre-read cost is the operator's call.
- **Corrected:** option G's cost line previously opened "the reason is
  evidential, not procedural" and then gave procedural reasons. Given the two
  corrections above, the procedural reason is the only sound one, and it is
  stated as such.
- **Resolved:** options B (note at read time), E (test-only) and F (fix the
  rendering) were absent from an earlier set that was tilted toward "edit a
  document," which is where its recommendation landed.

---

# Group 2 — adopter-facing correctness

## 4. D3 — Is the published container image adopter-facing, and where does that put `dashboard/`?

**Class:** authority (what the project holds itself to for what it ships).
**Reversibility:** reversible for three of five options; one is a one-way door.
**Blast radius:** surface for the declaration; fleet for the consequence riding on it.
**Decays:** no — but it is re-derived at full price on every touch.

### The fork — stated as two questions, because it is two

An earlier draft declared the first half "close to settled by evidence" and
narrowed the live fork to the second. That is not right, and the correction
matters: the operative test is a standard, and choosing it is the operator's.

**Q1: what makes something adopter-facing?** Two candidate tests are both live
in the repository, and they give opposite answers here:

- *Advertisement plus content* — the dashboard is advertised to adopters in
  three published documents and ships in the released image, therefore it is
  adopter-facing.
- *Is it a general-purpose adopter surface, or a reference/operator artifact?* —
  the test implied by the existing exemption, under which `agents/` ships in the
  same image and is exempt anyway.

**Q2, downstream of Q1:** if `dashboard/` is inside the fleet-neutrality
boundary, is it held there by a guard, by tests, or by rendered-output
assertions?

### Established

- **The main `Dockerfile` ships `dashboard/`**, and it is the main `Dockerfile`
  — not `Dockerfile.glama` — that the release workflow builds (`context: .` with
  no `file:`). The COPY list is `src/`, `governance_core/`, `agents/`, `config/`,
  `dashboard/`, `skills/`, `VERSION`.
- **`src/http_api.py` serves the dashboard at `/` as well as `/dashboard`** — the
  route table registers both against `http_dashboard_redesign`, with the comment
  "Root also serves the redesign." Corroborated in `README.md`,
  `docs/manual/03-running-the-server.md` and `docs/public-site/index.md`.
- **The publish workflow attaches SBOM, provenance and a pushed attestation.**
  It fires on release publication. Eleven releases exist (v2.13.0 through
  v2.22.1, none draft or prerelease); the publish workflow itself has seven
  successful runs, six release-triggered.
- **`COMPATIBILITY.md` binds the published container to a versioned adopter
  contract**, distinct from the source version.
- **The counterweight is real and decisive against a naive membership test.**
  The same image ships `agents/`, which `CLAUDE.md` exempts by name — and
  `agents/` is not inert there: `src/http_routes/sentinel.py` imports
  `agents.common.resolution_outcome` at runtime, putting an exempt, name-
  saturated tree on the shipped server's import path. Image membership is
  demonstrably not the operator's operative test today.
- **The public landing page carries the tree's only `docker pull` instruction**,
  locked by `tests/test_build_public_site.py`.
- **No documented adopter path actually *runs* the published image.**
  `docker-compose.yml` builds from source; the README quickstart and the public
  site's "Try the released surfaces" are clone-and-compose. The public site
  frames the pull as "inspect." So adopter-facing status, on the advertisement
  test, rests on advertisement and content — and the "identical content" clause
  needs one qualification (see the `node_modules` item below): CI and release
  builds are identical to the pulled image; a local compose build after
  `npm ci` is not.
- **No already-written standard settles it.** `CLAUDE.md` states a principle
  ("the boundary is the shipped artifact, not the repo") and an enumeration
  (`src/`, `governance_core/`, `config/`, `agents/sdk/src/`, tracking
  `[tool.setuptools.packages.find]`) that diverge for a non-Python surface — a JS
  tree structurally cannot appear in that list. A comment above `DEFAULT_PATHS`
  in `scripts/dev/check_fleet_identity_leak.py` says "Ops scripts, docs, tests,
  dashboards and plist templates are deliberately excluded," with a rationale
  argued about ops scripts specifically. The conflict is real; whether the
  comment's argument extends to dashboards is a reading.
- **The guard cannot currently be pointed at the dashboard.**
  `check_fleet_identity_leak.py` iterates `rglob("*.py")`. There are zero tracked
  `.py` files under `dashboard/`, so on a fresh checkout `--paths dashboard`
  scans zero files and exits 0 — a clean report over an unexamined tree, which is
  the failure its own coupling comment forbids. (In a tree that has run
  `npm ci` it finds exactly one file: a vendored npm dependency.)
- **`dashboard/redesign/snapshot.js` is a real capture of live governance
  state**, tracked and COPYed into the image. Its header: "Bundled real snapshot
  — pulled live from the governance server on 2026-06-19." It holds resident
  names and agent ids, per-day EISV trajectories with verdicts, dialectic session
  topics, an adjudication queue, and process rows carrying host family, plugin
  version and model name. Some fields are visibly synthetic placeholders, so it
  is a partially-sanitised real capture.
- **The snapshot is not passive.** `withFallback(liveFn, snapFn)` in
  `dashboard/redesign/data.js` returns the snapshot on any live-endpoint throw or
  null. The empty-roster case is safe (an empty array is not null, so live wins);
  an outage is not. The route is auth-gated, but the trusted-network bypass in
  `src/http_routes/access.py` covers six networks — loopback v4 and v6, Tailscale
  CGNAT, and all three RFC1918 blocks — and applies whenever strict REST auth is
  not required, which is the default when no MCP bearer is configured. The
  remedy is already named in `src/http_routes/dashboard.py`: "The deeper fix is
  for the fallback bundle to hold synthetic data instead of a real capture."
- **PR #2150's fix is narrower than it reads and its author said so.** Three
  summary panels are gated on roster membership; `residentPanels()` still
  hardcodes three resident-named endpoints and three name lookups plus a literal
  pairs list. The PR body defers this question explicitly and names both halves
  of the tension independently.
- **The dashboard's enforcement today is vitest, and it is recent.** The
  `dashboard` job runs lint and tests; its own comment records that six spec
  files added in Aug 2026 "ran nowhere." A spec already documents the snapshot
  hazard in prose but asserts only that the page survives a 401 on it.
- **Adjacent surface, same question:** `skills/` is also COPYed into both images,
  is also outside the guard, and names a resident in eight places in one file.
  Whatever answer D3 gets should be stated in a form that covers non-Python
  shipped trees generally, or the question returns for `skills/` next.

### Options

| # | Option | What it does | What it costs |
|---|---|---|---|
| **A** | **In, and guarded.** Declare the image adopter-facing; fleet-neutrality scope stops tracking `pyproject.toml` and starts tracking the image COPY list; a JS-capable scanner is written; the guard's dashboard exemption is deleted. | Mechanical enforcement on the surface an adopter lands on. | A net-new scanner, not a config change. A naive port is worse than nothing: the dashboard legitimately writes resident-shaped words in route strings and section headings, and the Python guard's own history records substring matching flagging 31 places of which one was real. It also forces the `snapshot.js` question immediately — a real capture naming residents cannot survive a neutrality rule — so A silently includes that work. Highest total cost. |
| **B** | **In, tested not guarded.** Declare the boundary and hold it by test, on the #2150 pattern: dashboard changes touching resident-shaped data carry an empty-roster / foreign-roster spec. Amend the guard comment to say the dashboard is in scope but out of this instrument's reach, and why. | Cheapest of the "in" options — zero new tooling, one paragraph, one comment correction. | Enforcement is author-authored: a hardcoded name in a *new* dashboard section is caught only if someone writes the spec for it. That is precisely the blind spot the fleet guard exists to close, accepted knowingly. Leaves today's hardcoded endpoints and lookups in place with no scheduled removal. |
| **C** | **In, enforced at the rendered output.** One residentless / foreign-roster jsdom spec that renders the page and asserts no roster identity string reaches the DOM. | Mechanical without a source scanner: a new hardcoded section fails it without anyone writing a spec for that section, and it is immune to the literal-matching problem that sinks a naive JS port, because it inspects output values rather than source tokens. The harness exists — one spec already drives `data.js` through a stubbed fetch under an empty roster, another builds a JSDOM document from the page source. | Not free: `residents.js` hardcodes all six names today, so such a spec starts red. It would have to ship as a scheduled-removal instrument (skipped or xfail with a dated removal note) rather than a green gate on day one — which is an honest cost, and also a smaller one than A's scanner. |
| **D** | **Out, and written down.** Declare the published image an operator/reference artifact; `dashboard/` is exempt like `agents/`. | Cheapest to write, ratifies the guard comment's current wording, and the archaeology stops because the answer is on the page. It is not self-contradictory: the manual's own line calls the dashboard a view that "gives **operators** a human view of the fleet," which is the reading this option depends on, and the guard's existing rationale — a deploy script for this operator's fleet is supposed to name this operator's fleet — is the same argument extended. | Leaves the landing page at `/` outside every correctness rule the project applies to what it ships, and leaves `snapshot.js` shipping the maintainer's live capture into every released image with no rule against it. Removes the standard that made the #2150 fix required, so the next such defect arrives with nothing behind it. |
| **E** | **Move the artifact instead of the rule.** Drop `COPY dashboard/` (or put it behind a build arg), so the boundary question is moot for the published artifact. | Routes degrade gracefully — the handler already returns a 404 JSON body when a file is absent. | The 712 KB of assets is not the cost. The cost is a product retraction: README, the manual and the public site all advertise the dashboard as a shipped capability, and `docs/PRODUCTION_SNAPSHOT.md` shows it. All three need correcting, and un-retracting a published capability claim later is costlier than making it. The only option that forecloses the others. |

### Recommendation (non-binding)

**On Q1 I have no recommendation to give** — which test defines adopter-facing
is a product-intent call, the two candidate tests are both live in the tree, and
the `agents/` counterweight shows the operator's operative test is not the
obvious one. Both readings are internally coherent; A, B and C follow from the
advertisement test, D follows from the reference-artifact test, and E sidesteps
both.

**On Q2, conditional on Q1 landing "in": C, then B.** The reason is that C gets
A's mechanical coverage at closer to B's price, and it is immune to the specific
failure mode the Python guard's history documents, because it asserts on
rendered values rather than source tokens. Named cost: C starts red and must
ship as a dated scheduled-removal instrument, which is a weaker thing than a
green gate and should be described as such. B is the honest floor if that is
more appetite than this deserves; it forecloses nothing in C.

One observation that shapes A and C either way: the spec that *enforces*
neutrality, `dashboard/tests/residents-empty-roster.test.js`, itself hardcodes
all six resident names in a literal list. Whoever builds a source scanner must
decide `dashboard/tests` scope on day one — a concrete instance of "a naive port
is worse than nothing."

### The snapshot item is a recommendation, not a rider

An earlier draft carried replacing `snapshot.js`'s real capture with a synthetic
bundle as a "no-regret engineering item, needing no answer to D3." That framing
is withdrawn. Whether shipping a partially-sanitised live governance capture
inside a signed public image is acceptable is a disclosure risk-appetite
judgement — the operator's — and the mitigating context is real: aggregate
production statistics and dashboard screenshots are already published in
`docs/PRODUCTION_SNAPSHOT.md` and the README, so the disclosure class is not new.
It is also not literally cost-free: the bundle spans 18 data domains whose
branches the existing vitest specs discriminate on, so a synthetic replacement
must preserve every shape those specs distinguish.

What is fair to say: it is required under A and C, effectively required under B,
and merely desirable under D — and it is the one item here that reaches an
adopter's screen rather than a contributor's habits.

### Reversibility

Reversible for A, B, C and D; **one-way for E**. The declaration is a paragraph
in `CLAUDE.md` and a line in a guard comment — nothing at runtime reads it, no
migration, no API change. A, B and C are reversible into each other and B→C→A is
additive. D is reversible on paper but carries a decay cost: every session that
reads "the boundary is the shipped artifact" next to a dashboard exemption
re-opens the question, and #2150 already paid that archaeology once and deferred.
E obliges correcting README, the manual and the public landing page; re-adding
later means re-asserting a publicly withdrawn capability claim. The `snapshot.js`
replacement is one-way in the only sense that matters — nobody will regenerate a
real capture — and that is the direction you want. No option touches server code,
the database, a migration, or the MCP surface.

### Blast radius

**Adopters** — everyone who pulls the published image or builds the same
`Dockerfile` through compose. The dashboard is their landing page at `/`, so this
decides whether what it renders about *their* fleet is held to a correctness
rule. Under the default posture (no MCP bearer → strict REST not required →
trusted-network bypass across six networks), a loopback or LAN adopter is served
`snapshot.js`, and any `/v1/residents` error renders the maintainer's residents
as theirs.
**Future contributors** — whether a change under `dashboard/redesign/` owes an
empty-roster spec, or whether #2150 was a one-off courtesy. This is the recurring
cost: with no answer, each session re-derives the question from the same two
conflicting sentences.
**This operator** — until `snapshot.js` is replaced, a partially-sanitised
capture of live governance state ships inside a signed, attested, publicly
pullable image. What is new relative to the already-published statistics is that
`docs/` is excluded by `.dockerignore` while `snapshot.js` is not, and that this
copy is *executed* as fallback data by the adopter's own dashboard.
**Not affected:** server behaviour, schema, migrations, the MCP tool surface,
release cadence. One narrow qualification to "no runtime consumer": no `src/`,
`governance_core/` or `tests/` file imports or parses any of these; the
references there are docstring and comment citations.

### Default if silent

Nothing blocks, and five things continue.

1. **No CI job will ever fail on a resident name in the dashboard**, and not
   because anyone decided so. Even a well-meaning future session that adds
   `dashboard` to `--paths` gets a green run over an unexamined tree.
2. **`snapshot.js` keeps shipping**, and every adopter whose `/v1/residents`
   throws on a default non-strict install sees the maintainer's residents as
   their fleet, badged as a snapshot. The empty-roster case is safe; the outage
   case is not, and no spec covers it.
3. **#2150 does not generalise.** Three panels are gated; three hardcoded
   resident endpoints and three name lookups remain, and the next dashboard
   section starts from zero with no standard behind it.
4. **The archaeology recurs at full price**, between two written sentences that
   contradict each other on this exact surface, with `agents/` exempted in the
   same paragraph as a counter-example.
5. **The `node_modules` bloat stays.** `.dockerignore` omits it; `dashboard/.gitignore`
   excludes it from git, so CI and release builds are unaffected, but a local
   build from a tree that has run `npm ci` bakes in 132 MB. The one-line fix was
   in #2150 and was reverted, because `.dockerignore` sits in the Elixir path
   detector's relevant-files list and adding a line pulls in six Elixir suites,
   three of which fail (#2152).

**What silence also buys, which an earlier draft omitted:** the recommendation
above says the mechanical instrument should come "once the redesign's section
layout has stopped moving." If layout instability is a reason to defer C or A, it
is also a reason silence is not purely lossy. Nothing here decays on a clock — no
release, migration or published contract depends on it. It is a decision that
keeps, which is exactly why it keeps being re-derived until it is written down.

### Corrections and flags carried

- **Corrected, and it was overstated by two orders of magnitude:** `snapshot.js`
  ships eight discovery summaries, not "1075 knowledge-graph discoveries." The
  1075 figure is an aggregate `discoveries.total` next to type and status
  histograms; the `list` array holds eight entries with summary text. The
  disclosure is real and the other domains verify, but the blast-radius severity
  rested on that figure.
- **Corrected:** `residentPanels()` fetches five endpoints, of which **three**
  are resident-named; the other two are roster-independent. An earlier draft
  said "five resident-specific endpoints" and then listed three.
- **Corrected, and it removes a named cost of option A:**
  `tests/test_residentless_install.py::test_guard_scope_tracks_the_packaging_include_list`
  asserts that every shipped package is scanned — a **floor**, not a lock. It does
  not forbid extra paths, so adding `dashboard` to `DEFAULT_PATHS` passes it
  unchanged. No "second axis" is needed. This also narrows the claimed conflict:
  the packaging enumeration constrains what the guard must *at least* cover and
  is silent on what it may additionally cover.
- **Corrected:** the pre-commit hook is a **tracked** repo file installed by
  `scripts/ops/install_git_hooks.sh`. What is machine-local is the installation,
  not the hook. There is no `.pre-commit-config.yaml`, this checkout's
  `.git/hooks` holds only samples, and nothing forces a contributor to run the
  installer — so `CLAUDE.md`'s "pre-commit + `Repo Scope Guard`" credit is
  accurate about the repo and optimistic about any given clone.
- **Corrected:** eleven releases exist; the publish workflow has seven successful
  runs, six release-triggered. The "six releases" figure attached the right
  number to the wrong noun.
- **Corrected:** "The deeper fix is for the fallback bundle to hold synthetic
  data" is in `src/http_routes/dashboard.py`, not in `snapshot.js`'s header.
- **Corrected:** the trusted-network list is six networks, not three — wider than
  an earlier draft claimed, against its own argument.
- **Corrected:** the Glama image is *not* a second published image — `glama.json`
  is two keys, its workflow never logs in to a registry and never pushes, and the
  deployment doc says no release exists. There is one published image today. Even
  if it were published, its services bind loopback and its only public transport
  is stdio, so its `dashboard/` would be inert weight. Both leads that framed this
  around a second image are refuted, and the refutation was itself re-verified
  (`push: true` appears in exactly one workflow file).
- **Flag carried:** the argument that option D "contradicts three published
  documents" was a characterization, not evidence. `README.md` and the manual are
  operator-facing, and the manual's own quoted line says "operators." D is the
  other side of an unsettled definition, not a self-contradiction, and it is
  presented that way above.
- **Flag carried, adjacent drift:** `.github/workflows/repo-scope.yml` says the
  fleet guard "reports the two known couplings on every run," while
  `KNOWN_COUPLINGS` now holds one entry. Not a D3 claim and it changes nothing
  here, but it is the same class of drift this entry is adjudicating.
- **Flag carried:** `scripts/dev/check-wave3-ode-prereq.sh` gates on the existence
  of one of the docs in entry 7's set — dormant today, but relevant to any
  archival rename there.
- **Resolved:** option C (rendered-output enforcement) was absent from an earlier
  set, which made "B over A" look forced; and Q1 was declared near-settled rather
  than presented as the fork it is.

---

## 5. D2 — What the two-word review phrase names, and what satisfies the gate it guards

**Class:** authority (whether an agent-run review may clear a production gate).
**Reversibility:** reversible; one option forecloses in substance.
**Blast radius:** surface, with one ring that actuates.
**Decays:** no clock — but the decision gets made by default on the next touch.

### The fork

In `docs/proposals/continuous-verdict-blending-v0.md`, one phrase names two
incompatible things: a three-subagent review an agent ran on 2026-06-26, and a
human-owner sign-off that was never given. Which one it means decides what
satisfies the still-open gate that `docs/proposals/verification-weighted-verdict-v0.md`
inherits for enabling the live `GOVERNANCE_VERIFICATION_FLOOR` actuator.

**A naming note for this file.** The guard's Rule 5 matches four literal review-
register phrases. To keep this packet clean under that rule, they are named here
descriptively rather than written out: the two-word review phrase (`council` +
`pass`), its `fold` variant, the `three-lane` variant, and the hyphenated
verification-lane name (`live` + `verifier`).

### Established

- **This checkout is shallow, so local git cannot establish authorship.**
  `.git/shallow` is present (listing two SHAs), `git rev-list --count HEAD` is 60,
  and the graft root adds the entire repository as new files. Provenance had to
  come from upstream.
- **The two lines are a day apart and face opposite in time.** The file was
  created 2026-06-26 by PR #1088 with the prospective gate section already
  present — including the parenthetical naming human owners — written *before*
  any review existed. PR #1094 (2026-06-27) added the retrospective status line
  and the v0.2 section describing the review that ran.
- **The lead that PR #1094 rewrote the earlier text is REFUTED.** The safety-
  constraint section is byte-identical between the creation commit fetched from
  upstream and the working tree at HEAD. #1094's three deletions are all in the
  status header. The collision was created by *not* editing — which matters,
  because it means nobody ever weighed the two readings against each other.
- **The author asserted both readings in one paragraph.** PR #1094's body says
  the review was run "before implementing it — this change class (governance
  verdict math) requires a [review] first" (treating it as discharging the gate)
  and closes by handing the same gate to the named human owners (treating it as
  still open).
- **The term is prose only — it is never an identifier.** Across all tracked
  Python there are zero identifiers, enum members, tool names, actions, table or
  column names for it; the ~199 occurrences are comments and docstrings. In
  `db/` the hits are SQL comments. The one machine-readable use in a unitares
  surface is none: an `authority` enum in a **vendored** third-party schema
  (twice), and some captured knowledge-graph note text in a JSON evaluation
  file — data, not dispatch.
- **The guard's stated justification for exempting the bare word is wrong, even
  though its behaviour is defensible.** `scripts/dev/check-repo-scope.sh` Rule 5
  and the public `docs/REPO_SCOPE.md` both justify the exemption as "real product
  vocab … the dialectic system." The dialectic's escalation vocabulary is
  **quorum**, not this word — `src/dialectic_protocol.py` defines `ESCALATE` with
  the comment "Escalate to quorum" and a `quorum_voting` phase — and that path is
  retired with zero writers, asserted by a test. The behaviour is still right
  (flagging the bare word would false-positive on ~199 code comments); the reason
  given is incorrect, and it is repeated in the public policy document.
- **The guard flags this exact file today, and it is not allowlisted.** Running
  the guard with `--files` on it reports a violation; a clean-tree run reports
  clean, because the guard collects files only from a diff or an explicit list.
  It is grandfathered only while untouched.
- **The effective gate is CI, not the local hook.** `.github/workflows/repo-scope.yml`
  runs the guard on pull requests and pushes to the protected branches, and its
  own header says CI exists because cloud sessions bypass the local hook. The
  hook is tracked but opt-in and bypassable, and is not installed in this
  checkout.
- **Repo-wide usage is near-unanimous that the phrase names a parallel subagent
  review.** Five other proposals gloss it that way, one using the word as a verb
  an agent performs. No other document parenthesises it with named human owners.
  One further instance uses the word for an owner group without the two-word
  phrase, in `docs/proposals/thread-trajectory-stitching-v0.md` — so "sole
  outlier" is true of the phrase and not quite true of the word.
- **A dated definition predates both lines by nine days.**
  `docs/proposals/operator-decision-packet-v0.md` (2026-06-17), finding 4: the
  review "is a *human-facilitated* three-subagent pass that terminates in
  **findings**; the **dialectic** is an *agent-driven* thesis/antithesis/synthesis
  protocol that terminates in **resolutions**." Classified against it: the
  retrospective lines *are* such a review; the prospective parenthetical is not —
  the definition has no owner-review sense, and the human is the facilitator, not
  a seat. Caveat: that document's own status is design-first with the runtime
  build parked, so it is a written definition of record for this form, not a
  ratified glossary.
- **The same document partitions decisions by authority, and the partition
  cleaves this gate in two.** It says such mechanisms "can only legitimately
  converge on decisions that *have* a ground truth — factual disputes, coherence
  checks, 'does the diff match the spec,' reversible bets," while "authority
  transition" and "irreversible / high-blast-radius" calls "should bottleneck on
  the operator." It also says "Today the [review] terminates in *findings*; the
  operator reads them and synthesizes the call."
- **"The owners" resolves to one person.** `.github/CODEOWNERS`: "UNITARES is
  currently maintained by one operator," followed by a single owner entry. The
  owner-reading has no group to convene, and no record of such a review exists
  anywhere in `docs/`.
- **The ambiguity already reaches a shipped actuator.**
  `docs/proposals/verification-weighted-verdict-v0.md` makes enabling
  `GOVERNANCE_VERIFICATION_FLOOR` in a live deployment "the [review]-gated act"
  and cites the same gate. The flag is real: `config/governance_config.py` reads
  it, defaulting false, at a `src/governance_monitor.py` call site.
  `docs/proposals/resolved/demotion-review-2026-08-16.md` re-affirms that
  enabling remains gated. **Context an earlier draft omitted:** a *second* flag,
  `GOVERNANCE_VERIFICATION_FLOOR_SHADOW`, defaults **true** and takes the `elif`
  branch — the verification path already runs in shadow by default; only
  actuation is gated.
- **The recorded review is not auditable against the definition.** The document
  claims three lanes and names two; PR #1094's body names none. The third seat is
  unrecorded.
- **Deleting the review prose has a real downstream cost.**
  `src/governance_monitor.py`'s `apply_verification_floor` docstring cites the
  invariant "the [review] protects (continuous-verdict-blending-v0.md #3)"; the
  `Dockerfile` cites the doc above its digest pin; the sibling cites finding #3
  once. Finding #3 exists only inside the v0.2 section.
- **The blast is bounded in one direction, and that boundedness is a code fact,
  not a severity verdict.** `apply_verification_floor` is one-sided
  (`_more_severe_verdict` + `max`), so a wrong enable produces false pauses on
  benign work, never a missed pause on dangerous work. Whether over-pausing
  benign adopter work is acceptable exposure is a risk-appetite question — and
  the sibling's own gate list already requires "a real false-positive-regression
  pass on a larger benign-coding corpus" before enable, i.e. the repo already
  treats false pauses as worth gating on.
- **This is one instance of a repo-wide condition.** 33 tracked files match the
  guard's register patterns; 22 of them are unallowlisted and will trip on their
  next edit (see entry 7).

### Options

| # | Option | What it does | What it costs |
|---|---|---|---|
| **A** | **One referent: the phrase means the subagent review, and the 2026-06-26 review closed the gate.** Delete the human-owner parenthetical; mark the gate discharged. | Consistent with the corpus and with the 2026-06-17 definition *of the phrase*. | It is **not** consistent with what that same document says about authority. A review discharging a load-bearing gate is precisely what "authority transition … should bottleneck on the operator" and "forcing a [review] to 'decide' it just launders a guess as consensus" foreclose. This is the only option that changes what an agent may do to a running deployment: it lets an agent clear the enable-gate on a review it convenes and synthesises itself. The review being credited is the one whose third lane is unrecorded. |
| **B** | **Two gates, two names.** Keep the phrase for the subagent review; rename the prospective gate to operator sign-off, in this file, in the sibling, in `docs/proposals/resolved/demotion-review-2026-08-16.md`, and in the `docs/proposals/README.md` status rows. | Both mechanisms survive with distinct names, and the enable-gate on the live flag is unambiguously a human act. | Four files plus two index rows — the largest edit surface here. Each edited file trips the guard, so the PR must also settle strip-or-allowlist for each. It converts a soft pending item into a named, visible commitment on one person's queue. |
| **C** | **Split the gate by act.** The review suffices for the merge-time safety-envelope check on the blend formula (a coherence check, which the 2026-06-17 partition calls legitimately convergent); operator sign-off is required for the live enable (an authority/blast act). | Derived from the standard the operator already wrote, and it is the only option that matches the partition rather than collapsing it. Smaller than B: the sibling's enable line and one sentence here. | It introduces a two-tier gate where the document has one, so the distinction must be stated crisply or it becomes a new ambiguity. It also still needs the phrase disambiguated wherever both senses appear. |
| **D** | **Perform or decline the sign-off now.** Since "the owners" resolves to the person reading this packet, the gate can simply be discharged or explicitly declined, dated, in the sibling. | Removes the open item entirely at the lowest edit cost of any option that resolves anything. | It answers the *gate* without answering the *word*, so the two-referent collision survives and the next reader hits it again. And discharging it is a live decision about a production actuator, which is exactly the class this packet must not pre-empt. |
| **E** | **Allowlist the file as a frozen record; change nothing else.** One line in `scripts/dev/repo-scope-allow.txt`. | Cheapest by far. And the allowlist's precedent genuinely fits better than an earlier draft allowed (see the correction below): its first block covers frozen technical records whose *subject* is not the review process, and `docs/proposals/README.md` marks this file parked. | It resolves nothing. The two referents survive intact, the ambiguity that reaches the live flag survives, and the same question returns on the next read. It also silences Rules 1, 2, 3 and 6 on that file forever (see entry 7). |
| **F** | **Fix only the propagating edge.** Do not touch this file. State in the sibling that enabling the flag requires operator sign-off, not a subagent review, and stop citing the older document as the gate's definition. | One file, and it closes the only path from this ambiguity to a running deployment. | Leaves the target file self-contradictory, so the archaeology recurs on every future reader — exactly what the packet form exists to remove — and the sibling's new wording contradicts its own citation unless that cross-reference is also cut. |
| **G** | **Strip the review register from the file entirely.** Rewrite the section as unattributed findings and rename the gate. | The guard's own stated remedy; leaves the file permanently clean with no escape hatch. | Erases the provenance of finding #3, which shipped code cites as the invariant the review protects — the docstring would then point at a document that no longer says who found it. Also removes the section's basis for superseding the v0 body it contradicts: its authority rests on having been a review. Recoverable only from upstream history, and this checkout is shallow. |

### Recommendation (non-binding)

**C.** The word is settled by a standard the operator wrote — the 2026-06-17
definition, nine days older than both lines, plus five documents using the term
the same way — so the phrase means the subagent review. But settling the word
does not settle the gate, and the same document that defines the word also says
what such a review may and may not decide. C is the reading that applies both
halves of that standard instead of one. It is also smaller than B.

Named cost: C introduces a two-tier gate where the document has one, and it
still needs the phrase disambiguated where both senses appear, so it is not a
one-line edit. **B** is the fuller version at four files plus two index rows.
**F** buys the safety-relevant half of either for a quarter of the edit surface
and leaves the naming question open. **E** is cheapest and resolves nothing —
and it is what the operator is most likely to get by default. **A** should be
weighed knowing that it converts a documentation cleanup into a permission
change on a live actuator.

### Reversibility

Mechanically reversible throughout — documents only, no code, no schema, no
migration, and nothing here is read by the 2026-12-01 instrument. Two
asymmetries, both stated as what they are rather than as properties of the code:

- **A forecloses the others in substance** — once it is written down that an
  agent-run review discharges the gate, re-tightening means telling an agent that
  a review it already ran no longer counts, and PR #1094's author has acted on
  that reading once in writing. How much weight a written precedent carries in a
  single-operator repo is a judgement about the operator's own willingness to
  reverse themselves, not a mechanical fact.
- **G is the hardest to undo in practice** — it deletes provenance that survives
  only in upstream history, and this checkout is shallow, so a future session
  would have to know to go to GitHub to recover what a shipped code comment is
  citing.

B, C, D, E and F are each undoable in one small PR.

### Blast radius

**This operator:** the archaeology cost is paid on every read of either
proposal — a status line saying a review happened and a gate line saying one is
required, in one file, unresolvable without upstream history a shallow checkout
does not have.
**Future contributors:** the register policy in `docs/REPO_SCOPE.md` and the
guard's Rule 5, which currently justify their own exemption with a claim the code
refutes; 22 unallowlisted tracked documents sit in the same condition.
**Adopters — the only ring that actuates:** whether an agent may enable
`GOVERNANCE_VERIFICATION_FLOOR` on its own review in a live deployment. The code
fact bounding it: the floor is escalate-only, so a wrong enable produces false
pauses, never missed pauses. Whether that is acceptable exposure is the
operator's call, and the sibling's own pre-enable gate suggests the project
already treats false pauses as worth gating on.

### Default if silent

The guard is diff-scoped, so the file stays grandfathered indefinitely — verified
both ways: a clean-tree staged run reports clean, and an explicit run on this file
reports the violation. The phrase goes on naming both a review an agent ran and a
sign-off the operator did not give, in one document, with a shallow-checkout
reader unable to tell them apart.

Two things follow, and the second is a forecast rather than a fact, marked as
such. First, verifiably: the next agent that wants the flag on in a live
deployment reads the sibling's gate bullet, follows the citation, finds a status
line saying the review already happened and a gate line saying one is still
required, and picks — and the permissive branch is the one already picked once in
writing. Second, plausibly: the guard fires on the next commit that touches this
file for any reason at all, and an agent mid-PR about something unrelated
resolves it the cheapest way available, which is option E, chosen inside a PR
about something else.

**"Decide later, when I next touch this parked document" remains a legitimate
choice** — the file is marked parked, nothing decays on a clock, and an earlier
draft's flat claim that "silence does not defer the decision; it delegates it"
overstates a behavioural prediction as a structural fact.

### Riders (orthogonal to the choice)

- **Correct the guard's stated justification.** Rule 5's comment and
  `docs/REPO_SCOPE.md` both justify exempting the bare word as dialectic product
  vocabulary. That is false — the dialectic's word is *quorum*, on a retired
  path — and the false statement is in a public policy document. Correcting it
  changes no behaviour and attaches to any option, including doing nothing about
  the gate. An earlier draft surfaced this as load-bearing evidence and then
  offered the operator no lever for it.
- **Archive rather than allowlist.** `docs/proposals/README.md` marks this file
  parked since 2026-06-27; moving it to `docs/proposals/resolved/` and
  allowlisting under the existing frozen-record block is available under B, C, E
  or F. The allowlist header's own first preference is "Prefer moving the file
  out over allowlisting."

### Corrections and flags carried

- **Corrected:** 22 unallowlisted tracked files trip the register patterns, not
  "roughly 26"; and four of the 33 matches are guard-self files, not six. The
  earlier arithmetic was also internally inconsistent. The allowlist-coverage
  claim (exactly seven files) is right.
- **Corrected:** the sibling cites finding #3 **once**, not twice. The
  substantive point — that a gutting rewrite strands live citations — holds, on
  the `apply_verification_floor` docstring and the `Dockerfile`.
- **Corrected:** tracked hits under `dashboard/` are two, both in one file; the
  larger figure came from an untracked scan dominated by `node_modules` license
  text. The conclusion is unaffected and arguably strengthened.
- **Corrected:** the "string-literal grep returns one docstring hit" claim does
  not reproduce — run as described it returns zero, and the mention it referred to
  is prose inside a docstring. The load-bearing conclusion (zero identifiers
  anywhere in tracked Python) reproduces exactly.
- **Corrected:** "the single machine-readable occurrence" is two, in the vendored
  schema, plus note text in a JSON evaluation file. The conclusion (never a key,
  enum member or identifier in a unitares surface) holds.
- **Corrected:** the effective gate is CI alone. The pre-commit half is opt-in,
  not installed here, and bypassable; "fails both" overstated it.
- **Corrected:** the grep for a record of an owners' review returns more
  references to the document than an earlier draft counted; none is a record of
  such a review, so the conclusion stands and the count does not.
- **Corrected:** option E's cost previously claimed the allowlist's criterion is
  proposals whose *subject* is the review process, and that using it here widens
  the hatch. The allowlist has **two** blocks: one for frozen technical records
  whose subject is *not* the review process, one for records whose subject is.
  E fits the first block's precedent, and this file is marked parked. A dislike of
  allowlisting was doing work the written rule does not support — which also
  suppressed the archive-and-allowlist rider now listed above.
- **Corrected:** `.git/shallow` lists two SHAs, not one. Everything else in the
  shallow-checkout fact reproduces.
- **Flag carried, and resolved in the text:** an earlier draft quoted the
  2026-06-17 definition's sentence about *what the word means* and omitted the two
  sentences in the same document about *what such a review may decide* — then
  called option A "consistent with the definition." That is true of the phrase and
  false of what A does. Option A's entry above states the conflict.
- **Flag carried:** "the exposure is availability and adopter trust, not a safety
  hole" was a severity classification wearing a code fact. The code fact is stated
  above; the severity call is handed over.
- **Resolved:** options C (split by act) and D (perform or decline the sign-off)
  were absent, and B converted D's availability into a *cost*. An earlier
  recommendation was also a sequenced plan over a non-exclusive option set, against
  the form's own "2–4, mutually exclusive" and "the operator can override in one
  move." The set above is a single pick.

---

## 6. D1 — Which dormant doctor schema-drift checks CI should run, and where

**Class:** taste (appetite for a new gate) plus one authority element (blocking vs. advisory).
**Reversibility:** reversible; no option forecloses another.
**Blast radius:** local to fleet depending on option.
**Decays:** no.

### The fork

The doctor has four schema-drift checks. Which of them should run automatically,
in which job — given that only one of the four can observe anything CI's database
is capable of showing, and given that the four currently run **nowhere** automatic
at all?

### Established

- **"CI has no database" is false.** `.github/workflows/tests.yml`'s shard job
  runs a real Postgres service and creates `governance_test`; the matrix is two
  interpreters × eight shards = 16 legs, each with its own service container. The
  job's own comment records that a DB was added because 333 tests across 29
  modules were silently skipping without one.
- **CI has never called any of the four.** The only doctor reference in any
  workflow is a DB-free Dockerfile tag check.
- **The `doctor` collector in the surface-findings workflow is advisory and
  dispatch-only**, defaults to a different collector, has no fail-on, and its job
  provisions no database. Its code comment says the doctor "is pure noise on a
  stock CI runner."
- **`check_column_drift` refutes the blanket tautology claim.** It reads
  `INSERT INTO schema.table (...)` fragments out of `src/` and `governance_core/`
  — reproduced independently at 225 column references across 29 tables — and
  compares them against `information_schema` on the live DB. The code side does
  **not** come from the migrations, so this is a genuine two-source comparison
  that a migration-built CI database renders exactly. Its three named incidents
  (2026-04-17, 2026-04-19, 2026-05-07) are all of that shape.
- **`check_schema_migrations` is tautological in CI on two of three branches.**
  The DB rows come from the same registry inserts the source parser reads, so
  `mismatch` cannot fire; the database is created fresh per job, so `unexpected`
  cannot fire. **`missing` can**: the test bootstrap catches a failing migration
  and prints it, deliberately "loud, not fatal," so a migration that does not
  apply leaves no registry row and no red build. Nothing else in CI catches that.
- **`check_constraint_drift` is near-tautological against a CI database, and its
  own docstring says so:** "``ensure_test_database_schema`` re-executes migration
  files IN FULL against the test DB, so the test population is correct by
  construction while production stays wrong. This is a property of the deployed
  database, and only a check against that database can see it." The residual CI
  signal is narrow: a declared constraint that does not survive a full replay
  (e.g. swallowed inside an exception-suppressing block — 15 migration files
  contain one). It also tracks only `ALTER TABLE … ADD CONSTRAINT`, and
  `db/postgres/schema.sql` contains zero of those.
- **`check_migration_checksum_drift` can never produce a *drift* signal in CI,
  under any bootstrap the repo ships.** The CI schema comes from a bootstrap that
  executes migration files directly and records no checksum, so every registry row
  lands with checksum NULL and the check returns PASS over zero anchored
  migrations. The two paths that do record hash the file they just applied — a
  file compared against itself. Its only possible signal is a database that
  outlives the checkout.
- **Its logic is already CI-gated; only the live comparison is not.**
  `tests/test_unitares_doctor_script.py` holds 165 tests with monkeypatched psql,
  covering all four checks. Arming adds the live comparison and nothing else,
  which is why the tautology question decides almost the whole value.
- **The biggest finding, and it is wider than "not in CI": the four run nowhere
  automatic.** `scripts/ops/doctor_findings.py` filters
  `if check.mode != "operator" or check.name in SKIP_CHECKS: continue`, and all
  four are declared `mode="local"`. So the operator findings job — the one already
  connected to the deployed database where two of the four are the *only* place
  they could ever fail — skips every one of them. That script's own docstring says
  "The doctor's operator-mode checks are good and they run nowhere"; the same
  sentence is now true of these four local ones, because the earlier fix was
  scoped to one mode.
- **With one exception nobody had named: the four *are* armed on exactly one
  path — the adopter installer.** `scripts/install/setup.py` spawns the doctor in
  local mode and turns every fail/warn into a remediation plan item. So a fresh
  adopter gets all four; this operator's automation and CI get none. The checks
  are adopter-armed and operator-dark.
- **The doctor's own docstring misdescribes its mode flag**, which is plausibly
  the root cause: it reads as though operator is additive over local, while
  `run_checks` selects `mode == "all" or c.mode == mode` — so operator mode runs
  operator checks *only*.
- **A fully-provisioned adopter database already exists in CI.**
  `.github/workflows/docker-quickstart.yml` brings up the real compose stack on
  every push and PR touching `db/postgres/**`, the compose file, the Dockerfile or
  requirements. The doctor is stdlib-only and needs only psql, which the runner
  ships. Its limit: the path filter excludes `src/`, so it would never fire for
  the code changes `column_drift` exists to catch.
- **The two classes want different homes because they have different triggers.**
  `column_drift` fires on a `src/` change; the migration-facing checks fire on a
  `db/postgres/` change. No single existing job covers both.
- **CI's test database is not the adopter's database.** The test bootstrap
  swallows failing migrations, skips two schema files, and records no checksums;
  both shipped adopter paths use `ON_ERROR_STOP=1` and a different file order.
- **`column_drift` is very likely green on arrival.** A static approximation that
  parses every `CREATE TABLE` body and `ALTER TABLE ADD COLUMN` across the schema
  and all 67 migrations, replays multi-clause alters, and matches the 225 INSERT-
  referenced columns, resolves all 29 tables and finds **zero** candidate missing
  columns. Two candidates from a cruder earlier pass were confirmed as parsing
  artifacts of multi-column alters. Stated as a static approximation, not a run:
  no live database was reachable here.
- **There is no per-check selection flag.** The doctor's argparse accepts only
  `--mode {local,operator,all}`, `--json`, `--no-color`, `--db-url`,
  `--redis-url`, `--attest`. Arming means adding a selector or writing an inline
  import block — the precedent already in the workflow for the Dockerfile check.
- **`docs/dev/DRIFT_LEDGER.md` states a preference, not a prohibition.** "New
  guard? … prefer wiring it into CI's DB-free `smoke` job." These are not new
  guards and require a live database, which that job has none of. The ledger's
  migrations row already classifies them honestly as operator-run.
- **The restraint the two recent PRs declare is real but is not written policy.**
  #2145: "This backs a stated contract; it arms no new standard." #2149: "None
  arms a guard, widens a scope, or newly enforces anything." A grep across every
  tracked Markdown file for such a rule returns nothing — not in `CLAUDE.md`,
  `AGENTS.md`, `CONTRIBUTING.md` or the drift ledger. **Whether merging those PRs
  adopted their self-restraint as policy is a question of intent, and it is the
  operator's to answer.**
- **This decision was not answerable before three commits ago.** #2149 split the
  expected-versus-accepted migration sets; before that, a correct fresh database
  reported a phantom missing slot. The commit records "Verified: a fresh database
  now passes."

### The axis an earlier draft decided silently

**Blocking or advisory is a separate question and it is the operator's.** A test
under `tests/` is blocking on day one: it joins an existing shard and every leg
feeds the required status context, whose name the workflow comment notes is fixed
by branch protection — which an agent cannot edit. A standalone workflow job is
advisory until someone edits that rule. An earlier option set named this axis
("armed vs. merely visible is a real cost difference the operator alone can
close") and then made every arming option blocking, leaving the risk-appetite
question with no representative. Advisory variants are cheap and available
(`continue-on-error`, a non-strict xfail, or the existing advisory collector given
a service block), and the reversibility evidence points that way: the drift ledger
records that two of its own rows were over-claiming CI enforcement until #2145, so
a *claimed* gate has already proved worse here than an honestly stated gap.

**Every option below is offered in a blocking and an advisory form. The choice
between them is the operator's and is not made here.**

### Options

| # | Option | What it does | What it costs |
|---|---|---|---|
| **A** | **Arm nothing; correct the ledger and stop.** Amend the drift-ledger migrations row to name which of the four CI could ever observe, so the next agent does not re-derive it. | Honours the restraint #2145 and #2149 declared, without asserting it as policy. One table row. | The live gaps stay open indefinitely. A migration that fails to apply against a fresh Postgres still merges green. Code naming a column the schema lacks still merges green — the class behind three incidents in three weeks. And the two deployment-only checks keep running nowhere, including on the deployed host the operator's own findings job already polls. |
| **B** | **Wire the operator findings job to run these four local checks. Touch CI not at all.** Either reclassify the deployment-only checks to operator mode, or widen the job's filter to a named allowlist. | Zero contributor blast radius, one edit, and the **only** move that reaches the deployed database where two of the four can actually fail. On the packet's own reasoning this is arguably dominant over any CI-only arming. | Reclassifying removes a check from `--mode local`, which is what the adopter installer runs, so an adopter's install report loses that line; widening the filter avoids the trade at the cost of a second mechanism. And if the deployed database is currently drifted — nobody has checked, because nothing has run these — this posts findings into the governance stream on the next cycle. |
| **C** | **Arm `column_drift` only, in CI.** One file under `tests/` that skips when the test DB is unreachable (the pattern 333 tests already use), bootstraps the schema, and asserts the check does not FAIL. | Arms the one check that is genuinely non-tautological in CI, and leaves the other three exactly as they are. Runtime negligible. | In blocking form it is a new way for someone else's PR to go red. It is **not** only triggerable by a genuine defect: because the test bootstrap swallows a failing migration, a bootstrap failure leaves the CI database missing a column the code correctly references, and the assertion goes red on whichever contributor's PR is running — a red build its author did not cause and cannot fix. Does nothing for the migration-facing checks. |
| **D** | **Split by observability: each check goes where it can see something.** `column_drift` → a CI test as in C. `schema_migrations` → a step in the docker-quickstart workflow against the real adopter database. `constraint_drift` and `migration_checksum_drift` → the operator findings job as in B, because their signal is a property of the deployed database. | The only option that closes both the CI gap and the operator-dark gap, and it matches each check to its trigger. | Three edits across three files. The docker-quickstart step is advisory until branch protection adds the context — path-filtered workflows need skipped-equals-success handling to be required at all, and only the operator can make that change. Needs an inline import block in both CI homes. Carries B's adopter-report trade. |
| **E** | **Arm all four in one new DB-backed CI job.** | Uniform treatment. | Buys one permanently meaningless green: `migration_checksum_drift` cannot produce a drift signal there under any bootstrap the repo ships, so the job reports a passing content-anchoring check over zero anchored migrations. That is instrumentation failing toward healthy — the posture the workflow states twice is forbidden and that #2149 just spent a fix removing from `column_drift`. `constraint_drift` adds a near-tautology on top. Duplicates a database CI already has twice over, and is advisory until branch protection is edited. |

### Recommendation (non-binding)

**D**, with **B** as the honest floor if D is more appetite than this deserves.
The evidence-sourced part: the four checks are not one thing, and treating them
as one is what makes this look expensive. Once separated by what each can
observe, three have an obvious home and the fourth has an obvious non-home. The
part that is appetite and not evidence: that three edits across three files is
proportionate here rather than one. Nothing in the code decides that.

B is worth weighing seriously on its own. It is one edit, has zero contributor
blast radius, and is the only move that reaches the deployed database where
`constraint_drift` and `migration_checksum_drift` can ever fail — which is the
finding the framing of this question did not anticipate. If the operator's
appetite for a new contributor-facing gate is zero, B alone closes the larger of
the two holes.

Note a placement correction relative to an earlier draft: `constraint_drift` is
routed to the operator findings job above, not to docker-quickstart. That draft
routed it to a freshly replayed database — precisely the "correct by construction"
population its own quoted docstring rules out — and justified it on a narrow
residual signal. On the docstring's own terms, the matching home is the deployed
host.

### Reversibility

Reversible under every option, and none forecloses another. Every move is
deleting a CI step, deleting a test file, or reverting a mode string; there is no
data migration, no external state, and no registered instrument (this touches
nothing the 2026-12-01 read selects). C is a subset of D, and D is E minus the
check E should not arm. The one asymmetric cost is social rather than technical:
a gate that fires red on another contributor's PR is expensive to un-ring — and
the repo has evidence in the other direction too, since a *claimed* gate has
already proved worse here than an honestly stated gap. The single genuinely
one-way element is definitional: whichever way this is answered will be read as
the precedent for whether the #2145/#2149 restraint is policy, because nothing
written down says.

### Blast radius

**A:** none — one documentation table row. **B:** this operator's deployed host
only; the findings stream gains checks nobody has run, so if the deployed schema
is drifted it says so on the next cycle. Plus the adopter install report, unless
the filter-widening variant is chosen. **C:** every contributor's PR, in blocking
form, via a new condition on a required status context — firing on a genuine
defect, and also on a bootstrap failure the PR author did not cause. **D:** C's
radius plus B's, plus an advisory signal on `db/postgres/**` PRs. **E:** one extra
runner per PR for every contributor, plus the reputational cost of a green check
that means nothing. Code surface throughout is
`scripts/dev/unitares_doctor.py`, which is not on `CLAUDE.md`'s single-writer
list; the migration surface is, and it was checked — zero open PRs at time of
writing.

### Default if silent

Nothing changes and nothing degrades on a clock — but the position is worse than
"operator-run" makes it sound. The four keep running nowhere automatic: not in
CI, and not in the operator findings job either, which filters on operator mode
while all four are declared local. Their sole live arming is the adopter
installer. So on this operator's own deployed database — the only place two of
them can ever detect anything — they fire only when a human types the command.

Nothing expires and the drift-ledger row already reads honestly after #2145, so
the documents will not drift back into over-claiming while this sits. What
accrues is exposure, not error: every merged migration is another draw against
the class where a migration applied from an in-progress working tree stayed
invisible for three months and was found only because someone asked; every merged
`src/` change another draw against the column-drift class. Waiting also does not
improve `migration_checksum_drift`'s evidentiary position — its unverifiable count
falls only as migrations are legitimately applied to a fresh database, never by
waiting. And the restraint stays what it is: two authors' statements about their
own PRs, not a rule anyone can look up, so the next agent re-derives the entire
tautology analysis from scratch. If the appetite for a new gate is genuinely zero,
**A is worth taking deliberately rather than by silence**, precisely because it
writes down the part that is expensive to rediscover.

### Corrections and flags carried

- **Corrected:** `tests/test_unitares_doctor_script.py` holds **165** tests, not
  157 — and it was never 157 in recent history. The coverage claim behind the
  number holds; the number did not.
- **Corrected, and it is an unreported finding:** `check_constraint_drift` has
  **no** fail-toward-unknown branch. Its constraint fetch returns None on any
  psql non-zero exit and the check maps that to SKIP, so it cannot distinguish an
  unreachable database from a queryable one. That is exactly the posture #2149
  spent a fix removing from `column_drift`, and the posture option E is faulted
  for. The other three do fail toward unknown; this one fails toward silence.
- **Corrected:** "`check_migration_checksum_drift` is structurally incapable of
  failing in CI" is false as worded. It **can** return FAIL — a psql failure other
  than a missing-column error becomes "could not read migration checksums — drift
  state is UNKNOWN." So arming it in CI is not a guaranteed permanent green; it is
  green-on-drift, red-on-infrastructure. The substantive point (it can never
  produce a *drift* signal in CI) is fully confirmed and is how the text above
  states it.
- **Corrected:** the open-PR claim was stale. At time of writing
  `CIRWEL/unitares` has **zero** open PRs. An earlier pass saw two, neither of
  which touched this surface; the conclusion was right and its evidence was stale.
- **Flag carried, could not be established from the repo:** the repository ships
  only a launchd plist **template**. Nothing in the tree establishes that the
  findings job is actually loaded on the operator's machine, and this environment
  cannot check. The *mechanism* is fully confirmed — the mode filter and all four
  `mode="local"` declarations — so **if** it runs, it skips all four. The text
  above says "the operator findings job" rather than asserting a running daemon.
- **Corrected, practical:** the doctor's default DB URL uses `localhost` while the
  compose file publishes on the IPv4 loopback address only. If `localhost`
  resolves to the IPv6 loopback first on the runner, psql fails — which renders as
  FAIL-toward-unknown for one check and SKIP for another. Any docker-quickstart
  step must pass `--db-url` explicitly rather than rely on the default. An earlier
  draft called the two URLs "verbatim" the same.
- **Corrected, cosmetic:** the surface-findings workflow's collector expression was
  quoted slightly wrong; everything else about that fact is verbatim-correct. The
  `CONTRIBUTING.md` section enumeration omitted a trailing section; the claim it
  supports — no rule anywhere about arming guards — is confirmed by grep.
- **Corrected:** option C's blast radius previously said the assertion "can only
  fire when code genuinely names a column the schema lacks, which is a defect
  either way." The bootstrap-failure vector is stated above.
- **Verified independently, recorded so it is known to be re-derived rather than
  trusted:** the 29-table / 225-reference figures reproduce digit for digit; the
  "green on arrival" conclusion holds under a better parser than the original; the
  migration-registry tautology analysis is exactly right, including a step not
  originally spelled out (the base schema's own registry insert is byte-identical
  to what the first migration declares, and every insert is conflict-tolerant, so
  `mismatch` genuinely cannot fire); all CI facts reproduce; the mode-selection
  logic and the misdescribing docstring both reproduce; and every quoted commit
  message and ledger row is verbatim.
- **Resolved:** option B (operator findings job only) was absent from an earlier
  set, even though that set's own headline finding was that the gap is wider than
  CI. Its absence made a three-edit CI split look like the only way to close
  anything. The blocking/advisory axis was likewise decided silently; it is handed
  over above.

---

# Group 3 — internal hygiene

## 7. D7 — The documents carrying pre-existing repo-scope Rule 5 violations

**Class:** taste, with one embedded severity question that is the operator's.
**Reversibility:** mostly reversible; one option is semantically one-way.
**Blast radius:** contributors and this operator; one sub-item reaches the public repo.
**Decays:** no.

### The first question, which an earlier draft answered on the operator's behalf

Rule 5 covers two different things in one bullet: the review-process register,
and operator-local absolute paths. `docs/REPO_SCOPE.md` writes them as **one
class with one rationale**, naming an operator home path in the same sentence as
the review-register phrases, and the guard's remedy text is portability-framed
("use `~`, `$HOME`, or an env var, never a machine-absolute path"), not
privacy-framed. The repository is genuinely public, and the path occurrences are
illustrative file URIs disclosing a username that is already the public repository
owner.

An earlier draft asserted that "the one adopter-facing defect in this whole set
is not the review register at all" — the path occurrences — and recommended
accordingly. That is a severity ranking, not a finding, and the operator has not
written it down. **So the first question is:**

> Do you treat operator-local absolute paths as higher-severity than the review
> register, or as one class the way `docs/REPO_SCOPE.md` writes them?

The answer changes which option below is cheapest-for-what-matters. It is stated
first rather than embedded in a recommendation.

### The second question

For the tracked documents that already trip Rule 5 — and so red-CI on any future
touch — do we batch-allowlist them, strip the register out of them, scope the
guard so legacy debt stops blocking unrelated edits, or first make the allowlist
rule-scoped so an entry added for the register stops silencing the other five
rules on the same file?

### Established

- **The flagged set is a property of the branch in flight, not a fixed repo
  state.** The guard collects files only from a staged diff, a base-ref diff, or
  an explicit list. There is no sweep-everything mode, no scheduled job, and no
  test asserting the tree is guard-clean.
- **Bounded by running the guard over every tracked file: 23 violations in 22
  files, all Rule 5.** No Rule 1/2/3/4/6 violation exists anywhere in the tree
  today. Reproduced for this packet.
- **21 of the 22 are frozen by the repo's own status tags; exactly one is tagged
  Active** in `docs/proposals/README.md` — with the caveat below that the mapping
  from those tags to "frozen" is a judgement.
- **Seven of the 22 sit in `docs/proposals/resolved/`, the repo's own shipped
  archive, and two files already in the allowlist live in that same directory.**
  Applying the written criterion to those seven is criterion application against
  its own precedent class, not a judgement.
- **The guard is file-level and content-level, never hunk-level.** A one-character
  edit anywhere in one of the 22 fails CI on a violation the edit did not
  introduce. The diff selects files; it never scopes the match.
- **`--diff-filter=ACMR` includes renames**, so the repo's own archival workflow —
  relocating a resolved proposal into `docs/proposals/resolved/` — is itself a
  guard-tripping action for the 15 non-resolved files.
- **"A gutting rewrite" overstates the strip cost for half the set:** only four
  exact phrases plus one path shape trip Rule 5, and bare product vocabulary
  passes. Eleven of the 22 carry two or fewer trigger occurrences.
- **Rule 5's regexes are CASE-SENSITIVE**, which changes what a strip means.
  Reproduced for this packet: across the 22 flagged files the guard sees **141**
  occurrences while the register's real footprint is **177**. Capitalised forms do
  not trip it at all. So a strip sized to the guard's hit count leaves the register
  visibly present, and a lowercase-only strip inside one file produces a
  self-contradicting document.
- **The register's real extent is wider than the guard-visible set**, and it is
  not confined to documents. Sixteen further tracked files carry only capitalised
  forms and are guard-clean today — including shipped source, several test files,
  an ops shell script, and Elixir tests. Reproduced by inverting the
  case-sensitivity above.
- **The allowlist is rule-blind, and that has already leaked.** The allow check
  short-circuits at the top of the per-file loop, *before* Rule 1 — so a path added
  for the register also exempts that file from Rules 1, 2, 3 and 6 forever. Not
  hypothetical: a file allowlisted 2026-06-28 for the register carries two
  operator-home-path occurrences that the entry silences, one of them a
  configuration value inside a plist snippet.
- **The allowlist's own default stance is "do not allowlist."** Its header: "Add a
  path here only when the scope guard flags it but it genuinely belongs in the
  agnostic repo (rare). **Prefer moving the file out over allowlisting.**"
  `docs/REPO_SCOPE.md` repeats it.
- **The criterion is stated three times, dated, with reasoning** — including
  (2026-08-13) "rewriting the ledger's past entries to satisfy the guard would
  falsify the record. New entries must still be written clean — the allowlist
  exempts the file, not the practice."
- **The effective gate is CI.** The tracked hook exists and runs the guard, but
  it is opt-in, not installed here, and bypassable; the workflow's own header says
  CI exists because cloud sessions bypass it. The `--no-verify` escape the failure
  path offers comes from the hook wrapper, not from the guard, so it exists only
  locally.
- **Nothing will ever surface this set proactively.** The 22 are invisible until
  someone touches one, at which point they surface as a red gate mid-task with a
  one-line remedy in the failure text and no criterion in front of the person
  deciding.
- **Two of the 22 are not records at all**, so the falsification argument does not
  reach them: two forward-looking gate statements about work not yet done, each
  the file's only hit. Rewriting them falsifies no past finding.
- **Five of the 22 are cited as design contracts from runtime source comments**
  (`governance_core/verification.py`, `src/governance_monitor.py`,
  `src/http_routes/effects.py`, `src/background_tasks.py`,
  `src/fleet_metrics/catalog.py`) and one test. None imports or parses them, but a
  full rewrite would leave those comments pointing at text that no longer reads as
  cited.
- **`scripts/dev/check-wave3-ode-prereq.sh` gates on the existence of one of the
  22.** The gate is dormant today (its precondition directory does not exist), but
  the archival rename that entry recommends elsewhere would silently remove what
  it looks for.

### Options

| # | Option | What it does | What it costs |
|---|---|---|---|
| **A** | **Batch-allowlist all 22 under a fourth dated criterion block.** | One commit to the allowlist. Every latent trip clears at once. | Because the allow check short-circuits before Rule 1, all 22 become permanently exempt from Rules 1/2/3/6 — including the operator-home-path occurrences, which go from "flagged on next touch" to "silenced forever." Allowlists the one file both written sources call living. Takes the list from 7 to 29 entries, against its own header's "rare" and "prefer moving the file out." Cheapest to execute, weakest fit to what was written. |
| **B** | **Allowlist the frozen files; strip the Active one.** | Applies the criterion against the README status tags. | The Active file carries 18 register occurrences, many inside dated frozen sections embedded in a living document, so a literal strip rewrites past findings inside a file classed living — the falsification the 2026-08-13 note forbids, reached from the other direction. Inherits A's rule-blindness for every entry added. Needs the two lease-plane files adjudicated (see the flag below). |
| **C** | **Strip the register from all 22; add nothing to the allowlist.** | Maximal fidelity to the guard's stated purpose. | Most of the guard-visible occurrences are a **lane attribution**, not a stylistic tic — which reviewer found what, in dated records. That is precisely what the operator wrote "would falsify the record" about, and it re-opens a criterion already decided three times. And because the regexes are case-sensitive, stripping the 141 guard-visible occurrences does **not** leave the tree clean of the register: 36 more sit in the same 22 files in capitalised form, and 16 further tracked files — including source and tests — carry only capitalised forms. Largest diff, highest archaeology cost, only option that forecloses the others. |
| **D** | **Make the allowlist rule-scoped first, then apply B under the narrowed form.** | Accept `<path> <rule>` entries, defaulting a bare path to all-rules for backward compatibility; re-tag the existing seven; add the frozen set as rule-5-only. Strip only what the criterion does not cover. | Touches the guard script, which has no test today — roughly a day rather than an hour. Does not by itself settle the lease-plane pair or the Active file; it makes those calls safe to defer, not decided. Adds a second column to a format written by hand. |
| **E** | **Scope the guard to the diff, or add a baseline ratchet.** | The standard remedy for legacy lint debt: match only added lines, or check in the 23 known violations as a baseline and fail only on new ones. | Clears all 22 latent gates, adds zero allowlist entries, keeps every future violation caught, preserves every record verbatim — and, decisively, requires no answer to the frozen/living question at all. Cost: it is a real change to a guard with no test, and a baseline file is a second thing to keep honest. A whitespace-only reflow of a legacy line would read as "added" under line-scoping. |
| **F** | **Strip only the operator-home-path occurrences; defer the register question entirely.** | Roughly seven occurrences across two files — five in one flagged document, two in a file the allowlist has already silenced. | An hour-scale change needing no decision about frozen versus living, no guard change, and no allowlist edit. But it answers only the sub-item whose severity is the open first question above — so if the answer to that question is "one class," F addresses a fraction and leaves the rest exactly as it is. |
| **G** | **Move the frozen records out of the public repository**, per the allowlist header's own stated first preference and the guard's own remedy text. | The operator's written first-preference remedy, applied. | Whether frozen review records belong in the agnostic repo at all is a product-intent call. It loses the records from the repo's own history-in-place, and five runtime source comments cite five of these documents. Named here because dropping it from the set settles it silently in favour of keeping them. |
| **H** | **Do nothing; decide per-file at next touch.** | Whoever next edits one reads the criterion and picks, as happened three times already. | Every touch — a typo fix, a status refresh, or the normal archival rename — becomes a red gate on a violation the author did not introduce, decided under time pressure with the failure text's one-liner as the only guidance. That is how the existing allowlist grew, and the rule-blindness compounds silently with each ad-hoc entry. Zero cost today; the bill arrives repeatedly at the least convenient moment. |

### Recommendation (non-binding)

**E**, and it is a recommendation whose *inputs* are the guard's own confirmed
behaviour, not a severity ranking. The reason: this entry's own established facts
say the harm is that "a one-character edit anywhere in one of the 22 fails CI on a
violation the edit did not introduce." E is the option aimed straight at that
harm. It clears every latent gate, adds nothing to a rule-blind allowlist,
preserves every record verbatim, and — this is what distinguishes it — requires no
answer to the frozen/living question, which is the expensive judgement everything
else in this list depends on. Option D already puts editing the guard on the
table, so "we shouldn't touch the guard" is not a reason to prefer another.

Named cost: E changes a guard that has no test today, so it needs one; and a
baseline is a second artifact to keep honest. **D** is the right second choice if
the rule-blindness matters more than the legacy debt — it is the only option that
stops an entry added for the register from silencing four other rules. **F** is
worth taking under any option *if* the answer to the first question is that
operator paths outrank the register; it is severable and cheap. **A** is cheapest
and fits the written rule worst. **C** should not be taken without first reckoning
with the case-sensitivity finding, which means it does not do what it claims.

Two sub-calls need no decision either way and ride any option: **the seven
`resolved/` files** are the same class as two paths already allowlisted under the
written criterion, and **the two forward-looking gate lines** record no past
finding. On that second one, one qualification: an earlier draft called them
"lossless." One of them specifies which review lanes must run before the build, so
neutralising it changes the content of a live gate, not just its register. Small,
but "lossless" is the wrong word.

### Reversibility

Mostly reversible. Allowlist entries are single lines, and the guard re-flags a
file immediately on removal. D's rule-scoping is additive and backward-compatible.
E is a guard change, revertible in one commit, though a checked-in baseline
outlives its revert unless deleted with it. Strips are reversible in git but
semantically one-way: once a lane attribution becomes "refuted on review," the
attribution is gone from the readable record and restoring it means PR
archaeology — the cost the 2026-08-13 note names. C is therefore the only option
that forecloses the others for the files it touches. Nothing here decays on a
deadline: no read, gate, or registered instrument depends on it, which is why this
entry ranks last in the packet.

### Blast radius

(1) **The public repository** — the operator-home-path occurrences are already
published and stay published under A, B, E and H. Whether that is the item that
matters most is the first question above. (2) **Future contributors and agent
sessions** — 22 files are latent red gates, and the surprise lands on whoever
touches the file, not on whoever created the violation. (3) **This operator** —
the archaeology cost per ad-hoc decision, and the growth of a rule-blind
allowlist. No runtime, no schema, no test and no registered instrument is touched
by any option, with one narrow exception: one dormant shell gate checks for the
existence of one of the 22 files, which matters only under an archival rename.

### Default if silent

Nothing fails today and nothing will fail on its own. No branch is in flight
against any of the 22 — this branch's diff against master is empty and there are
zero open PRs — the guard is diff-scoped with no sweep mode, no scheduled job, and
no test asserting cleanliness. So the set stays invisible until someone touches a
file. None of the 22 was touched in the 59 commits available in this shallow
clone; **that window is three days long, so it measures nothing about how dormant
these documents are.** It establishes only that the default is currently costing
nothing observable, not that it is safe.

What silence buys over time: (a) the two operator-home-path occurrences in the
already-allowlisted file stay published permanently, because no future touch can
ever flag them; (b) the five in the largest flagged file stay published until
someone edits it, at which point they surface bundled with 48 register hits and
will most likely be cleared together by one allowlist line, silencing them too;
(c) archiving any of the 15 non-`resolved/` documents stays blocked, because a
rename is a change under the guard's file filter; and (d) the allowlist keeps
growing one entry at a time, written under gate pressure by whoever is mid-task,
which is how all three existing blocks came to be. Locally, the realistic outcome
is a `--no-verify` bypass and a red build in CI, since the escape exists only on
the local path.

### Corrections and flags carried

- **REFUTED — a stated finding was itself false.** An earlier draft reported that
  `CLAUDE.md`'s hot-phase list "names a file that does not exist,"
  `beam-coordination-kernel.md`. The file **exists**, at
  `docs/ontology/beam-coordination-kernel.md`, and is tracked. `CLAUDE.md` lists
  it as a bare filename alongside an explicit `docs/proposals/` path, so the drift
  is a wrong implied directory, not a missing file. It is also Rule-5 clean, which
  is why it is not among the 22.
- **Corrected:** the two lease-plane files carry 48 and **11** register
  occurrences, not 48 and 8 — the second figure was a line count, mixed into a
  sentence of occurrence counts. The Active file carries **18**, not 17. An
  internal inconsistency (47 vs 48 for the same file) is resolved at 48 register
  occurrences plus five path occurrences.
- **Corrected, and it is material to option C:** the Rule 5 regexes are
  case-sensitive. Reproduced for this packet at 141 guard-visible of 177 actual
  occurrences in the 22 files, plus 16 further tracked files — source, tests, an
  ops script, Elixir tests — carrying only capitalised forms and therefore
  guard-clean. "Strip 141 and the tree is fully clean" is false; it leaves the tree
  guard-clean while the register remains visibly present.
- **Corrected:** "triples the list (7 → 29)" is a 4.1× increase. The direction of
  the argument is unaffected. Corrected: the `--no-verify` remedy comes from the
  hook wrapper, not the guard's failure text, so the predicted silent outcome is
  right for a different reason. Corrected: the ADR's status tag is quoted from the
  index, which reads "Closed · Accepted."
- **Flag carried, and stated as the first question above:** the severity ranking
  of operator-local paths over the review register is not written down anywhere;
  `docs/REPO_SCOPE.md` writes them as one class. An earlier draft used the ranking
  as the premise of its recommendation.
- **Flag carried:** "mechanical for 21 files" is an inference, not a classification.
  The written criterion says "frozen technical records, not living product docs";
  the index says Parked, Built (partial), Built (dormant) — and several of those
  tags explicitly anticipate future edits ("DO NOT BUILD YET … Build-trigger =",
  "the execute half is not cleared", and one whose single Rule-5 hit *is* the
  not-yet-done gate line). Mapping those tags to "frozen" is a judgement about
  which documents will be edited again. The frozen/living mapping is handed to the
  operator as the call it is — including the two lease-plane files, where the index
  says frozen and `CLAUDE.md`'s single-writer list says hot-phase.
- **Flag carried:** the BLAST claim that "no test is touched by any option" is
  literally true (the source references are docstring and comment citations), but
  one dormant shell gate checks for one of the 22 files by path. Recorded above.
- **Resolved:** options E (diff-scope or baseline), F (paths-only strip) and G
  (move the files out — the operator's own written first preference) were absent.
  Their absence made D the cheapest option protecting the thing an earlier draft
  had decided mattered most, which is the option set doing work the operator should
  be doing.
- **Legitimate and not re-litigated:** applying the three dated criterion blocks to
  the seven `resolved/` files is criterion application against its own precedent
  class. The rule-blindness finding is a mechanism fact and is reported as one. And
  the refusal to read a three-day commit window as evidence of dormancy is the
  measurement-authority discipline applied correctly — no capability is proposed
  for retirement on any count anywhere in this packet.

---

# What this packet could not establish

Seven things. Each is named with why, so the operator knows which are questions
about the repository and which are questions only they can answer.

1. **Whether the restraint declared in PRs #2145 and #2149 is standing policy
   (entry 6).** It exists only in those two commit messages. A grep across every
   tracked Markdown file for such a rule returns nothing — not in `CLAUDE.md`,
   `AGENTS.md`, `CONTRIBUTING.md`, or `docs/dev/DRIFT_LEDGER.md`. Whether merging
   them adopted their self-restraint as a rule is a question of intent, and it
   needs the operator's memory rather than the repository.

2. **Whether the operator's findings automation is actually running on the
   deployed host (entry 6).** The repository ships a launchd plist **template**
   only. The mechanism is fully confirmed — the mode filter, and all four checks
   declared local — so *if* it runs, it skips all four. Whether it runs cannot be
   checked from this environment and is not in the tree.

3. **Whether the deployed database is currently drifted (entry 6).** Nobody has
   checked, because nothing has ever run these checks against it. Two of the four
   can only ever detect anything there. Establishing it needs a live database this
   environment cannot reach.

4. **The December cohort's per-agent cluster geometry (entry 2).** The number
   that decides whether a within-agent null can even be formed — total `Null
   clusters` per agent — was dropped from the frozen transcription and is recorded
   as unrecoverable without a live re-run the power audit says is not justified.
   The stop rule separately forbids refreshing the condition-3 feasibility
   diagnostic with live data before the read, and this packet did not.

5. **Whether `column_drift` is green against a real database (entry 6).** No live
   database was reachable here. The "green on arrival" statement is a static
   approximation over the schema and all 67 migrations, and is labelled as one
   everywhere it appears.

6. **Authorship and edit history of the two colliding lines (entry 5), and
   provenance generally.** This checkout is shallow — 60 commits, a graft root
   that adds the whole repository as new files — so local `git log` and `git blame`
   establish nothing about any file older than three days. What could be
   established came from upstream. Any future claim about when a line was written
   needs a full clone.

7. **Which operative test defines "adopter-facing" (entry 4), and whether
   operator-local paths outrank the review register (entry 7).** Both are product
   intent, both have two coherent readings live in the tree today, and neither is
   written down. These are not gaps in the investigation; they are the decisions.

One further note on scope, stated because it bounds every entry above: **no
option in this packet was tested by running it.** Every option set is derived
from files read directly and from commands run read-only. Nothing here edited the
pre-registered stop rule, ran any analysis script against live data, or wrote
anything the 2026-12-01 read will select.
