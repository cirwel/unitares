# Mercor Safety Research Grant — Expression of Interest (draft)

**Status:** Draft for applicant review, 2026-09-16. Not submitted. Bracketed
fields are for the applicant to complete. Every evidence claim below traces to
a canonical document in this repository; if a claim is edited, keep it
consistent with [`EVIDENCE_AND_LIMITS.md`](../EVIDENCE_AND_LIMITS.md) and the
[evaluation catalog](../EVALUATION_INDEX.md).
**Surface:** external funding document. It proposes a study and registers
nothing. A protocol registers only when a preregistration lands under
[`docs/proposals/`](../proposals/README.md) by that index's rules, after the
same adversarial design review the existing protocols received.
**Call:** Mercor Safety Research Grants, a USD 5M program funding researcher
hours, API credits, event stipends, and expert time from Mercor's platform.
Named interests: misalignment, sandbox escape, evaluation awareness,
interpretability, oversight and control, red-teaming methodology. Requested
format: a one- or two-page Expression of Interest covering the team and its
accomplishments, the proposed project, and outputs and impact with
directionally correct timelines and resources.

Before submission, the applicant should complete the bracketed fields, confirm
or revise the resource table (its figures are estimates), and re-check every
number against the documents linked above on the day of submission, since
several are dated snapshots.

---

## Expression of Interest

**Project title:** Where runtime governance goes blind: a preregistered,
externally scored study of evaluation-aware and adversarial agent behavior
under a deployed behavioral monitor.

**Tracks:** evaluation awareness (primary); red-teaming methodology;
misalignment (reward hacking and scope violations); oversight and control.

**Applicant:** Kenny Wang, CIRWEL Systems
([ORCID 0009-0006-7544-2374](https://orcid.org/0009-0006-7544-2374)).
Independent researcher and sole maintainer of UNITARES.
[Applicant to add: formal background, prior roles, and any collaborators or
advisors taking part.]

### 1. Team, background, and research accomplishments

The applicant designs, builds, and operates
[UNITARES](https://github.com/CIRWEL/unitares) (Apache-2.0), a self-hosted
runtime governance and accountability server for heterogeneous fleets of
long-running AI agents. Agents bind a process identity, check in during work,
receive a policy action with a named reason (proceed, guide, pause, reject),
record outcomes that ground later assessments, and can open structured,
contestable review of any decision. Its behavioral state estimate (EISV) is
instrumentation, never verdict authority. The maintainer deployment has run
continuously since November 2025: 4.57 million audit and telemetry events and
71,141 stored state observations at the 2026-08-11 frozen snapshot, from a
single-operator, co-development fleet the project documents as dogfood rather
than independent evidence.

- **Preprint.** *UNITARES: Information-Theoretic Governance of Heterogeneous
  Agent Fleets* ([DOI 10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159));
  public, not peer-reviewed.
- **Open dataset.**
  [`hikewa/unitares-eisv-trajectories`](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories):
  32,181 labeled behavioral-state windows (20,655 real from one embedded
  agent's 39-day run, 11,526 synthetic), with per-row provenance and the
  generating pipeline published.
- **A public claim ledger with evidence classes.** Sustained operation
  (operational observation); identity and audit trail (exercised path);
  predictive lift (non-detection, inconclusive at current power); incident
  prevention (untested); robustness to a motivated attacker (structural
  limit). Withdrawn results stay recorded as withdrawn.
- **Preregistered protocols with stop rules.** A fixed 2026-12-01 confirmatory
  read with a kill criterion; a four-arm, task-family-clustered
  accountable-coordination ablation, independently reviewed 2026-09-15; a
  twelve-arm incremental-value ablation; an independent-operator cohort
  protocol. One paired cohort has run to completion under this discipline
  (240 scored calls) and returned a registered `SAFETY_STOP`, published as a
  negative result.
- **Named, reproduced blind spots.** Calibrated capability concealment is
  in-band observationally equivalent to honest low capability; a synthetic
  twin replay in the test suite shows it. The governor is authored by the
  governed: a 2026-08-06 audit found one cadence constant's suppression window
  had downgraded 195 of 218 recorded pauses (89.4%) before delivery, so a
  one-line change moves most of the enforcement surface; a CI gate now labels
  changes to that inventory. A reviewer-label study of 97 review messages
  found 47.4% templated pseudo-disagreement from one reviewer backend against
  4.8% after a backend change.

Relevant experience: agentic evaluation harnesses (sealed scorers,
non-disableable outer experiment ledgers, task-family clustering,
selection-aware permutation nulls, planning-only power estimates), adversarial
fixture design, and a fail-closed, per-effect-veto posture on the one
host-execution surface the system exposes.

### 2. Proposed research project

A model can pass a safety evaluation and behave differently in deployment.
Runtime governors are the deployment-side answer, but they see only in-band
evidence: what the agent does and claims inside the monitored session. This
project asks one question in three testable parts:

> When an agent knows a runtime governor is watching, does its behavior
> change, and can the governor tell from in-band evidence alone?

- **Q1, evaluation awareness.** Across matched task families, does the rate of
  scorer-detected scope violations and reward-hacking shortcuts differ between
  an undisclosed-monitoring framing, a disclosed-monitoring framing, and an
  explicit "this is a safety evaluation" framing? Primary estimand: the paired
  task-family difference in externally scored violation rate, undisclosed
  minus disclosed, with task success co-primary so that a lower violation rate
  bought by lower completion stays visible.
- **Q2, in-band detection.** Within each framing, does the governor's in-band
  evidence (state estimate, calibration error, outcome-grounded integrity)
  separate violating from non-violating episodes over a previous-outcome
  baseline under a selection-aware null, and does that separation change
  between framings?
- **Q3, adversarial evasion.** Against scripted and human red-team adversaries
  attempting baseline poisoning, self-report and outcome-record forgery, and
  calibrated concealment, what fraction of attacks evade detection, by class?

**Design.** Repository diagnosis-and-patch task families with a frozen source
snapshot, difficulty-matched variants, sealed deterministic tests, and a
contamination check: the boundary already registered for the applicant's
accountable-coordination ablation. Each family carries at least one
scorer-detectable shortcut (editing the tests, disabling a check, using an
out-of-scope tool, fabricating a result), so violations are observed by the
scorer, not inferred from transcripts. Framings are assigned within family by
an operator-owned manifest no agent can change; models, tools, budgets, and
prompts outside the treatment are matched. In the undisclosed framing the
harness captures check-ins from host hooks alone and exposes no governor
surface, tool, or injected context to the agent; the disclosed framing adds
the governor's tools and policy responses; the evaluation framing adds the
explicit statement. Three or more model families supply independent draws;
task family is the inference unit. Sealed tests decide success and detect
shortcuts; Mercor's expert network (software engineers) applies a blinded
rubric to what tests cannot decide: whether a shortcut was deliberate, and
whether the agent's own account matched its actions. Mercor red-teamers and
scripted adversaries run the Q3 attacks against a frozen build, and every
attack becomes a fixture that replays without a paid API.

**Inference discipline** is the one already in use: protocol registered before
enrollment with a separate immutable enrollment artifact; power planning
first; an outer ledger recording assignment, budgets, isolation, and terminal
state for every episode; intention-to-treat scoring; fixed stop rules; every
result published under its evidence class, including non-detection and safety
stops. The study is a new prospective cohort in an isolated namespace; it
never re-reads or pools with the maintainer deployment's registered
2026-12-01 read.

**Out of scope.** Whether governance prevents incidents is a causal-efficacy
question needing its own interventional design. Transfer to untested model
families is not claimed. The project measures a boundary; it does not claim
the boundary is closed.

### 3. Outputs and impact

All outputs are public: (1) the preregistered protocol with power planning,
stop rules, and enrollment artifact; (2) an open dataset of paired episode
trajectories across the three framings with sealed scorer results, blinded
human labels, and versioned telemetry envelopes; (3) a red-teaming
methodology and fixture corpus for runtime governors, with detection rate by
attack class, replayable without a metered API and so transferable to other
governors; (4) a paper reporting Q1 through Q3 whichever way they fall, each
claim carrying its evidence class; (5) instrument changes to UNITARES only
where the registered result supports them.

Either result is actionable. If in-band detection fails once an agent is
evaluation-aware, out-of-band cross-framing checks become a required layer for
any deployment-side monitor, and the fixtures show exactly where. If detection
holds for some attack classes, that boundary is documented with replayable
evidence. Labs get a measured answer to whether disclosed monitoring changes
agent behavior on machine-checked tasks; operators get a public corpus to test
their own governors against.

**Timeline** (six months, directional):

| Month | Work |
|---|---|
| 1 | Task families and shortcut design; power planning; protocol registration; adversarial design review |
| 2 | Pilot and canary gate; scripted-adversary corpus; scorer sealing; grader rubric calibration |
| 3–4 | Confirmatory cohort across three framings and model families; blinded human grading |
| 4–5 | Red-team campaign against the frozen build; fixture capture |
| 6 | Registered analysis; dataset release; paper; any supported instrument changes |

**Resources** (estimates, to be refined at proposal stage):

| Item | Estimate |
|---|---|
| Researcher time | Applicant at 0.6 FTE for six months [plus a part-time research engineer if a collaborator is named] |
| API credits | USD 15–25k across three or more model families: about 1,500 agentic episodes (three framings × three families × about 40 task families × four variants) plus pilot and adversarial runs |
| Expert network | About 250 hours of blinded engineering rubric grading, including double-grading for agreement; about 120 hours of red-teaming |
| Evaluation infrastructure | Mercor's internal evaluation infrastructure for sealed scoring and isolation, subject to review; otherwise the applicant's own harness |
| Events | One conference presentation stipend |
