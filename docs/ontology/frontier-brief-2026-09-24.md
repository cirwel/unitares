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
     discovery in advance. If that holds here, curated context would push the
     protocol's D-minus-B contrast toward zero. That is a choice for the
     operator at enrollment (§3).
3. **Only two of the brief's four proposed assessable claims hold today without
   narrowing:** identical retries return one canonical outcome, and conflicting
   reuse of a key is rejected. "No governed mutation without a current permit"
   holds only when `UNITARES_GOVERNED_EFFECT_BINDING` is on, and only for effects
   routed through the governed-effect plane. The server has no single tool that
   reconstructs an artifact's provenance. Scope both claims down before they
   appear in any assurance case (§2).
4. **The oversight summary is the one item that is actually new, but two of its
   four terms cannot be computed today.** "Action coverage" needs to know how
   many actions happened in total. Anthropic knows that because every action on
   its platform passes through an inline monitor. UNITARES sees only the
   check-ins and effects that are sent to it. "Human-resolution status" needs a
   recorded human-or-model resolver class, which exists only on the Sentinel
   adjudication path (§1).

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

- *Review latency and escalation rate* can be derived from records UNITARES
  already keeps: dialectic sessions, review records, and outcome events. A search of `src/` for `review_latency`, `escalation_rate` and
  `oversight` found no existing rollup. A name search can miss a rollup built
  under other terms, so treat this as "not found", not as "absent".
- *Human-resolution status* is not derivable today. Dialectic sessions keep a
  `reviewer_agent_id` and a terminal resolution, but nothing records whether the
  resolver was a human or a model. Operator versus model adjudication is recorded
  only on the Sentinel path (`src/http_routes/sentinel.py`, #2378). This term
  needs a recorded resolver class before it can be reported.
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
| No governed mutation occurs without a current permit | Content-bound, single-use, short-TTL effect grant (`src/effect_grant.py`), verified and nonce-consumed in `src/http_routes/effects.py` | **Only when `UNITARES_GOVERNED_EFFECT_BINDING` (or a per-type flag) is on, and only for effects routed through the governed-effect plane.** Minting is off by default and returns `501 binding_not_enabled`. |
| Every multi-agent artifact has reconstructible identity and provenance | Attributed writes, lineage, review records, export history | **No.** By design, the server has no single reconstruction tool: clients assemble records across retention and authorization boundaries. The claim has to name a retention window and a record set before it can be assessed. |

**Recommendation.** Make the first assurance case the outcome-binding claim. It
combines the first two rows and is scoped to the retention window. The brief's
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
that work is already done for all of them, and the primary D-minus-B contrast is
pushed toward zero.

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

- The coordination-ablation receipt records "attempted and completed effect
  types, authority verdicts".
- The governed-effect plane's veto produces `governance_blocked`.
- Compensation is the plane's promotion requirement
  (`docs/proposals/active/governed-effect-plane-v0.md` §5b).

Two states have no recorded field today: **refused**, meaning the model declined
before attempting, and **recovered**, meaning compensation was applied. Before
proposing a new schema, map the ladder onto the existing receipt and verdict
fields and name only those two gaps.

The software-analogue experiment is not authorized by anything here. It would
need its own registration, because it involves destructive operations even in a
sandbox. A caution for anyone designing it: RoboHarm's refusals clustered
entirely on the one explicitly violent wording. A software analogue built from
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
