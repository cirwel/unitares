# Frontier brief, 2026-09-17 to 2026-09-24: source check and repo mapping

**Status:** Research note. This note does not register or amend anything, and it
does not authorize a build, a run, or a change to the positioning prose.
**Date:** 2026-09-24.
**Input:** an operator-supplied weekly brief, *Frontier AI systems brief —
September 17–24, 2026*. It covers four items: Anthropic's R&D-automation
measurements, OpenAI's principles for third-party assessment, the WorkWorlds
evaluation infrastructure, and the RoboHarm embodied-refusal benchmark. Its
thesis is that the field is converging on persistent identity, action-level
telemetry, pre-registered claims, replayable evidence, and effect-boundary
enforcement, so UNITARES should make agent work independently assessable rather
than add features.
**Companions:** [`competitive-analysis-2026-09.md`](competitive-analysis-2026-09.md)
(market map and claim classes) and
[`competitive-survival-audit-2026-09.md`](competitive-survival-audit-2026-09.md).
This note does not revise either one.

**Method.** Each factual claim in the brief was checked against the primary
source where this session's egress policy allowed. Each recommendation was then
compared with what the repository already has. `arxiv.org`, `openai.com`,
`robocurve.org` and `the-decoder.com` were blocked. The Anthropic page and the
WorkWorlds and RoboHarm GitHub READMEs could be opened. The cells below say which
kind of source backs each claim. Search-snippet evidence is not byte-verified and
must be re-opened before anyone cites it elsewhere.

---

## TL;DR

1. **The brief is mostly accurate. It has three errors that matter:**
   - Its WorkWorlds figures do not match the released artifacts.
   - It omits the human-to-human baseline that puts Anthropic's 59% agreement
     figure in context.
   - It reads Anthropic's model-independent agent identity as support for
     UNITARES. Anthropic keeps one identity per agent across model upgrades.
     UNITARES mints a new identity for each process on purpose, so this part of
     the evidence is an alternative design, not support (§1).
2. **Two of the four recommendations are already built or registered.**
   - Exactly-once outcome binding shipped in #2246.
   - The WorkWorlds-style coordination ablation is registered as
     `accountable-coordination-ablation-v0` (#2248, registered at #2249).
   - What WorkWorlds adds is a gap in that protocol. It never states whether
     each arm's starting context is task-curated or the full state visible to
     the agent's role. WorkWorlds found that curation largely solves evidence
     discovery in advance. If that holds here, curated context would hide
     whatever C and D contribute to *initial* discovery. It would not remove
     their handoff contribution, because the protocol's forced discontinuity
     still requires carrying state a predecessor discovered. That is a choice
     for the operator at enrollment (§3).
3. **Only two of the brief's four proposed assessable claims hold today, and
   both only within a stated scope:** identical retries return one canonical
   outcome within the binding retention window, and conflicting reuse of a key
   is rejected on two surfaces (outcome binding and orchestrator spawns), not
   platform-wide. "No governed mutation without a current permit"
   holds only when `UNITARES_GOVERNED_EFFECT_BINDING` is on, or the per-type
   flag for that effect type is, and only for effects routed through the
   governed-effect plane. Complete provenance reconstruction is unmeasured,
   not disproven: clients assemble it across retention boundaries, and the
   claim names no window or record set. Scope both claims down before they
   appear in any assurance case (§2).
4. **The oversight summary is the one item that is actually new, but only one
   of its four terms, review latency, can be computed today.** "Action
   coverage" needs to know how many actions happened in total. Anthropic
   knows that because every action on its platform passes through an inline
   monitor. UNITARES sees only the check-ins and effects that are sent to it.
   "Human-resolution status" needs an authenticated, session-level resolver
   class. Review verdicts can carry an optional reviewer stamp, but it is
   caller-declared on every path except the in-process synthetic reviewer,
   and it is not authenticated. Only the Sentinel adjudication path separates operator
   from model verdicts by authenticated route, and it does so per finding,
   not per dialectic session. "Escalation rate" has no single stream: the
   stored `escalated` status has no writers, and pauses split into four
   populations with different denominators (§1).

---

## Source check

| Brief claim | What the source says | Source opened | Verdict |
|---|---|---|---|
| Anthropic: Claude "leads" 26% of model-R&D work and collaborates on >90% | 26% lead as of August; "above 90%" at or above the collaborates level; not fully autonomous for any measured subset | [Anthropic, *Measurements for understanding the pace of AI development inside frontier labs*](https://www.anthropic.com/institute/measuring-pace-of-ai-development) | Confirmed |
| ~30,000 concurrent agents on the main internal research platform | "Approximately 30,000" | same | Confirmed |
| Every action passes through an online monitor; >1B August decisions; 0.002% blocked | "100% of these agents' actions pass through the online monitor"; "0.002% (about 1 in 47,000)" of "over a billion decisions" | same | Confirmed |
| Offline monitoring flags ~100,000 transcripts weekly; ~50 reach human review | "roughly 100,000 transcripts per week", "approximately ~50 per week" escalated | same | Confirmed |
| Identities persist across model upgrades; messages and artifacts are attached; inter-agent communication is cross-referenced | Identities persist "through model upgrades, so an agent's record is continuous"; "shared, open messaging system", cross-referenced | same | Confirmed |
| Frozen 378-category task tree from sampled staff records | 20% of staff sampled per department per week in July; ~15,000 tasks; a 542-node tree with 378 leaves, frozen | search snippet (secondary coverage) | Consistent; not byte-verified |
| Model/human agreement 59% exact, 97% within one level | Same figures. **The source also gives human/human agreement at 35%, which the brief omits.** | Anthropic page | Confirmed. The omission matters: model/human agreement is higher than human/human agreement, so 59% says more about how hard the rating task is than about the model rater. |
| Independent evaluators promised, not yet evidenced | Plans to "embed independent third-party evaluators from multiple organizations" | Anthropic page | Confirmed |
| OpenAI, Sept 22: narrowly scoped, pre-registered claims; proportionate access; disclosed conflicts; transparent methods | Four priority areas (safety cases, critical safeguards, Preparedness capability evaluations, misalignment-incident investigation) and seven principles, including pre-registered claims, proportionate access, transparent method, independence and conflict-of-interest disclosure, and responsible publication | search snippets only; `openai.com` blocked | Consistent on the principles. **Not verified:** the brief's scope items "training incentives" and "monitor disablement", and the phrase "editorial independence" |
| WorkWorlds, Sept 20: 192 matched runs, eight pharmaceutical-workplace assignments, three agents | "192 matched evaluations", "8 measured tasks across 6 employee seats" at PharmaCo | [agent-evalscience/workworld README](https://github.com/agent-evalscience/workworld) | Runs and tasks confirmed. **"Three agents" not found.** |
| Curated context raised sufficient-evidence access 72.8% → 90.4% and criterion success 68.0% → 76.7% | README: evidence access 72.9% (full) vs 90.4% (curated); criterion pass 68.2% vs 76.9%. An arXiv abstract snippet gives 74.5% and 79.4%, possibly from a different version; the abstract was not opened. | README; arXiv snippet | **The brief's numbers are wrong in the first decimal place and do not match either source.** The direction and the conclusion stand. Cite the README until the paper is opened. |
| Once evidence was accessed, success was 84.2% vs 82.7% | 82.7% curated vs 84.2% full | README | Confirmed. The brief gives the pair without saying which condition is which. |
| Frozen manifests, sealed verifier, append-only events, released sanitized evidence; production implementation restricted | De-identified ledgers, statistical code and an illustrative implementation are released under CC BY 4.0; the production implementation and organizational data are restricted | README | Confirmed |
| RoboHarm, Sept 18: three policies, five harmful tasks, 100 trials each; Astra 2 refused / 60 completed; Fable 5.1 20 refused / 34 completed; all Fable refusals on the baby-doll task | Same figures, 20 trials per task cell | secondary press snippets only; `robocurve.org` blocked | Consistent across several outlets; primary not opened. **The brief also omits the third policy:** MolmoAct2 (a VLA model) refused 0 and completed 6. |
| RoboHarm released code, videos, transcripts and trial data | The repo README says it provides "the benchmark and collection tools; it is not a frozen results dataset" | [robocurve/roboharm README](https://github.com/robocurve/roboharm) | **Partly unverified.** The code is released. The trial data may be published elsewhere, but this session did not find it. |
| No frontier lab disclosed a new RLHF, reward-model or data-curation recipe this week | — | not checked | Not checked; an absence claim across all labs is out of scope for this note |

---

## Mapping to the repository

### §1 Anthropic: identity, monitoring, and the oversight summary

**Where it converges.** Two things match: messages and artifacts are attributed
to an agent identity, and every action is checked before it runs. The first
matches UNITARES's attributed writes. The second matches the governed-effect
veto only in part: the veto checks effects routed through the governed-effect
plane, and ordinary harness actions can bypass that plane entirely. Anthropic's pipeline, where 100,000 transcripts are flagged, about 50 go to
humans, and roughly 1 in 47,000 actions is blocked, is a working example of the
"record everything, escalate little" shape.

**Where it diverges, and the brief should not blur it.** Anthropic's identity is
per agent and continuous across model upgrades: "an agent's record is
continuous." UNITARES is strict about identity in the other direction. A fresh
process gets a fresh `uuid`, and `parent_agent_id` / lineage records *inherited
work*, "not authority or cross-process sameness". That wording is from the
governance MCP server's own instructions; see also `docs/ontology/identity.md`
and the *Strict Identity, Simple Contract* block in `CLAUDE.md`. A continuous
record across processes comes from derived rollups
(`docs/proposals/active/principal-rollup-v0.md`), never from a stored identity
that outlives the process.

Anthropic's disclosure is therefore evidence that *a continuous per-agent record
is operationally valuable at scale*. It is not evidence for model-independent
selfhood in UNITARES's sense. Whether UNITARES should offer a record that
survives a model upgrade as a first-class view is a question for the identity
surface. That surface is single-writer, so this note raises the question and
does not answer it.

**Oversight summary: the brief's "best action".** The brief proposes
`action coverage + review latency + escalation rate + human-resolution status`,
broken down per agent, campaign, model version and effect class. The terms are
not equally available:

- *Review latency* can be derived from records UNITARES already keeps:
  dialectic sessions and review records. A search of `src/` for
  `review_latency`, `escalation_rate` and `oversight` found no existing rollup. A name search can miss a rollup built
  under other terms, so treat this as "not found", not as "absent".
- *Escalation rate* is a definition gap, not a query. The storable
  `escalated` dialectic status is council-retired and has no writers
  (`src/dialectic_db.py`). Migration 066 documents four distinct pause
  populations — circuit-breaker trips, lifecycle pauses, review pauses, and
  shadow evaluations that never actuate — each with its own denominator. It
  warns that a rate with a shadow denominator reads as inert while the system
  is intervening. The operator has to choose the stream and denominator before
  the rate means anything.
- *The proposed breakdowns are not all available.* The dialectic schema has no
  campaign, model-version, or effect-class column, so reporting per campaign,
  model version, or effect class needs those dimensions recorded first.
- *Human-resolution status* is not derivable today. Antithesis and synthesis
  messages *can* carry a reviewer stamp (`observed_metrics.reviewer_backend`).
  It comes from the optional `reviewer_provenance` argument, which restricts
  `reviewer_kind` to `agent_submitted`, `external_consult`, `orchestrated`, or
  `in_process_synthetic`, or it is written by the server for the in-process
  synthetic reviewer (`src/mcp_handlers/dialectic/handlers.py`). On
  antithesis, a caller may also pass `observed_metrics.reviewer_backend`
  directly, and that value is stored unchecked; the orchestrated reviewer
  uses this route (`agents/dialectic_reviewer/reviewer.py`). Synthesis accepts
  only the vetted argument. Verdicts with none of these carry no stamp. The code calls
  the stamp "descriptive provenance, not identity proof": it records a
  submission route as declared, not who resolved the session. So it is a place
  a resolver class could live, not evidence of one, and adding a `human` value
  to it would not close the gap. What is missing is an authenticated,
  session-level resolver class. One option is to set it only through an
  operator-authenticated route (an operator credential or passkey session), as
  the Sentinel adjudication endpoint already requires for its per-finding
  verdicts (`src/http_routes/sentinel.py`, #2378). Whether "human" should mean
  "operator", or any authenticated human reviewer, is the operator's decision.
  The `awaiting_facilitation` flag (migration 053) marks sessions routed to
  human facilitation; it does not record who resolved them.
- *Action coverage* has no denominator. UNITARES observes what adapters and hooks
  submit. It does not see the harness actions that bypass it. A coverage figure
  computed as reported ÷ reported is always 100% and means nothing. The honest
  version is coverage of the governed-effect plane (effects routed ÷ effects
  observed by the custodian), labelled as that. Any wider figure needs an outer
  instrument, like the one the coordination ablation already requires (§3).
- Under *Measurement authority* in `CLAUDE.md`, any such summary is telemetry.
  A low escalation or review count must not be read as "oversight unused" until
  the four zero states have been ruled out.

### §2 OpenAI: assessable claims and the first assurance case

The brief suggests four candidate claims. Here is where each one stands:

| Candidate claim | Existing mechanism | Holds today as stated? |
|---|---|---|
| Identical retries yield one canonical outcome | Prediction-bound `outcome_event` is exactly-once per `(agent_id, prediction_id)` while the binding is retained. Identical retries return the canonical outcome. (#2246, `docs/CHANGELOG.md`) | Yes, **within the binding retention window**. An expired prediction ID can start a new canonical submission. |
| Conflicting idempotency-key reuse is rejected | Outcome binding returns `PREDICTION_REUSE_CONFLICT`. Orchestrator spawns fail closed when a key is reused with a different spec, and reservations persist (#1939, #1942, #1953; migration 068) | Yes, for those two surfaces. It is not a platform-wide property. |
| No governed mutation occurs without a current permit | Content-bound, single-use, short-TTL effect grant (`src/effect_grant.py`), verified and nonce-consumed in `src/http_routes/effects.py` | **Only when `UNITARES_GOVERNED_EFFECT_BINDING` is on (or the per-type flag for that effect type), and only for effects routed through the governed-effect plane.** Minting is off by default and returns `501 binding_not_enabled`. |
| Every multi-agent artifact has reconstructible identity and provenance | Attributed writes, lineage, review records, export history | **Unmeasured, not disproven.** The server has no single reconstruction tool by design: clients assemble records across retention and authorization boundaries. `PRODUCT_DEFINITION.md` records complete reconstruction as unmeasured. As worded, the claim is underspecified; it has to name a retention window and a record set before it can be assessed. |

**Recommendation.** Make the first assurance case the outcome-binding claim. It
takes the outcome-binding half of the first two rows (identical retries, and
`PREDICTION_REUSE_CONFLICT` on conflicting reuse) and is scoped to the
retention window. Orchestrator spawn idempotency is a separate mechanism and
stays out of this case. The brief's
instinct is right here, and the mechanism already has adversarial tests and a
named known limit. That limit is #2247: when a commit is ambiguous, the outcome
can persist while calibration is delivered zero times. #2247 belongs in the case
as a disclosed limit, not something fixed first to make the case look clean.

Write the first case as a document using a template: claim, threat model, frozen
revision, assumptions, adversarial tests, evidence manifest, known limits, and
reproduction steps. Do not build a runtime `AssuranceCase` object yet. Designing
the schema before one case exists would be the feature breadth the brief warns
against. Where the case lives, and whether outside assessment is sought, is for
the operator to decide.

### §3 WorkWorlds: context assembly is part of the evaluated system

The brief's "best experiment" has already been designed and registered, in a
stricter form, as `docs/proposals/registered/accountable-coordination-ablation-v0.md`
(#2248; four arms from independent to fully accountable; task family as the
inference unit; a frozen snapshot; a forced process discontinuity; an outer
ledger that cannot be disabled). The brief does not mention it.

WorkWorlds exposes one variable that the registered protocol leaves unnamed: how
each arm's starting context is assembled. Arm A receives a "matched initial
source bundle". The enrollment gate freezes "source, prompt, harness … digests"
but never says whether that bundle is curated for the task or is the full state
visible to the role. WorkWorlds found that curation raised evidence access by
17.5 points while success given access stayed flat. Most of the headline gain
therefore came from discovery that curation had already done.

Arms C and D add durable findings, handoff and reconstruction, which are
discovery and transfer mechanisms. If every arm starts from curated context,
the *initial* discovery is already done for all of them, so the contrast cannot
show whether C and D help agents find evidence in the first place. Curation does
not do the transfer work, though. Every task has a forced process
discontinuity; arm B may not keep messages across it, while C and D carry
findings and reconstruction through it. A curated starting bundle does not hold
what the predecessor discovered, so that part of the D-minus-B contrast
survives. Curation narrows what the primary contrast can show. It does not, on
its own, push the contrast toward zero, and this note does not predict its
size.

This is a standard that decides the outcome, so under *Measurement authority* it
is stated here as a choice rather than applied:

- **(a) Full role-visible state for every arm.** Freeze the regime in the
  enrollment artifact. The enrollment gate already has to freeze source and
  prompt digests, so this may fit without a protocol amendment. Whether it does
  is itself the operator's reading of the protocol.
- **(b) Context regime as a crossed factor.** This doubles the cells and needs a
  pre-enrollment amendment. The protocol's amendment log is still empty and no
  cohort is enrolled.
- **(c) Keep curated context and disclose it.** The result is then conditional on
  discovery being solved in advance, and a null D-minus-B cannot be read as "no
  coordination value".

Recording an evidence-access measure in the outer ledger would be a new endpoint
under any of the three options. It is not on the protocol's list of secondary
outcomes, so adding it would be an amendment, and it is not proposed here.

### §4 RoboHarm: refusal is not a control plane

This agrees with the governed-effect plane's premise: the permit decides whether
an effect happens, not the model's stated intent.

The brief's five-state ladder is `refused → attempted → blocked → completed →
recovered`. Most of it already has a home:

- The registered coordination-ablation protocol *specifies* a receipt with
  "attempted and completed effect types, authority verdicts". It is a field
  table in a draft protocol with no cohort enrolled, and nothing in the code
  implements it yet.
- The governed-effect plane's veto produces `governance_blocked`.
- Compensation is the plane's promotion requirement
  (`docs/proposals/active/governed-effect-plane-v0.md` §5b), and it is already
  recorded. The `file_write` executor restores the pre-image and calls
  `EffectRepo.tombstone/1`, which sets `effects.payloads.rollback_state` to
  `tombstoned`. A dirty surface is recorded as `quarantined` instead (migration
  052, `elixir/lease_plane/lib/unitares_lease_plane/effect_repo.ex`).

Two states are recorded in running code today: **blocked**
(`governance_blocked`) and **recovered** (`rollback_state = 'tombstoned'`).
**Refused**, meaning the model declined before attempting, has no field
anywhere. **Attempted** and **completed** exist only in the ablation receipt's
specification, which is not implemented, and that specification does not list
rollback state. Before proposing a new schema, map the ladder onto the
recorded fields and the specified receipt, and name the gaps: no refused
state, an unimplemented receipt for attempted and completed effects, and
rollback state missing from the receipt's specification.

The software-analogue experiment is not authorized by anything here. It would
need its own registration, because it involves destructive operations even in a
sandbox. A caution for anyone designing it: per secondary coverage (the
primary source was not opened), all of Fable 5.1's refusals came on the
baby-doll task, the one that asks for direct violence against a human-like
target, and none on the other four hazards. A software analogue built from
"superficially benign" instructions measures the permit, not the model, which is
the intended target. Say so in its claim.

---

## Not done here, deliberately

- **No README or `PRODUCT_DEFINITION.md` edit.** The brief suggests the
  positioning "evidence and authority substrate for independently assessable
  multi-agent work." Reader-facing prose is a single-writer surface, and the
  product sentence is pinned by `PUBLIC_POSITIONING_CHECKS`. A competing phrasing
  is the operator's decision.
- **No edit to the registered ablation.** §3 states the choice. Amending the
  protocol is the operator's act.
- **No identity-surface change.** §1 raises the question of a view that survives
  model upgrades and leaves it there.
- **Watch items carried from the brief:** Anthropic's third-party evaluator
  embedding, OpenAI's first assessments under the new principles, WorkWorlds
  replication beyond PharmaCo's eight tasks, and RoboHarm with varied wording
  and longer horizons.

---

## Operator disposition (2026-09-25)

The five open questions above were put to the operator after this note merged
(#2400). The operator answered "idk" to each, asked whether a council could
settle them, and then delegated them in these words: *"proceed best for the
federation's future."* On resolver identity specifically, the operator said:
*"im thinking human is operator but maybe so can ai, idk."*

This section records that delegation and how the authoring session used it.
The session drew on a three-member subagent council (architect, adversarial
reviewer, live verifier), advisory local consults, and one governed dialectic
(session `5039af21a55b7d9f`). **None of these is independent review, and
agreement among them is not operator authorization.** The delegation above is
the only authority for the dispositions below, and it covers only these five
items. The operator can reverse any of them.

| # | Question | Disposition | What it does not do |
|---|---|---|---|
| 1 | Starting context in the coordination ablation (§3) | **Deferred until an enrollment is scheduled.** At that point, the context regime must be fixed by a logged pre-enrollment amendment, not by an enrollment digest alone. The amendment must also fix matched non-treatment retrieval tools across arms, a frozen truncation policy recorded for each run, evidence access as a descriptive outcome only, and a disclosure that the regime was chosen after the WorkWorlds numbers were read. The council leaned towards full role-visible state; keeping curated context and disclosing it remains a legitimate choice to make then. | It does not amend or re-register `accountable-coordination-ablation-v0`, enroll a cohort, or choose the regime now. |
| 2 | First assurance case (§2) | **Adopted.** Drafted as [`docs/evaluations/assurance-cases/outcome-binding-v0.md`](../evaluations/assurance-cases/outcome-binding-v0.md). The claim is scoped to the canonical outcome record within the retention window, and #2247 is disclosed as a limit. | It does not fix #2247, seek outside assessment, or create a runtime `AssuranceCase` object. |
| 3 | Oversight summary (§1) | **No summary is built.** One principle is recorded for whenever a resolver class is designed: *record who resolved a review truthfully by kind: operator (authenticated by operator credential or passkey), other authenticated human, or AI agent or model. An AI's decision is never recorded as the operator's.* A metric then chooses which kinds count; for now "human-resolved" means operator only. This matches the Sentinel path, where model verdicts stay separate from operator labels. The escalation stream and its denominator stay open. | It does not change the schema or any code, and adds no metric, threshold or dashboard. |
| 4 | Positioning wording | **Held** until the assurance case in row 2 has been assessed by someone other than the authoring session. | It makes no edit to the README or `PRODUCT_DEFINITION.md`. |
| 5 | Continuous record view (§1, D5) | **Held.** Most agents do not yet declare a predecessor, so adapters declaring lineage comes first. If the view is built later, it should follow declared lineage only, show forks, label links "declared", mark links the parent has not confirmed, carry no trust or calibration, label `model_type` as self-declared, and return no root ID that can be used as a key. | It makes no change to the identity surface. |
