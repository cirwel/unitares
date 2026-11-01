# Evidence and limitations

UNITARES has a documented record of process attribution, shared findings,
governed review, and recovery across sessions. Captured server responses show
these mechanisms working; repository records show review findings becoming
concrete fixes and a review condition surviving its original session.

This page brings those results together with their sources, dates, and scope.

## Claim ledger

### Current claim status

| Capability | Demonstrated or observed result | Evidence |
|---|---|---|
| Process attribution and outcome binding | Calls and stored findings are bound to the process that made them. An outcome grades the intended prediction and retains its evidence grade. | [Captured identity and write sequence](guides/AGENT_SESSION_WALKTHROUGH.md#process-a) |
| Cross-session retrieval | A second process retrieves the first process's resolved finding, including its author and recorded closure, then declares the handoff. | [Captured successor sequence](guides/AGENT_SESSION_WALKTHROUGH.md#process-b) |
| Governed review | A paused agent's submission cannot resolve its own review over a standing reviewer objection; the objection remains pending for facilitation. | [Review constraint implementation](../src/dialectic_protocol.py) |
| Review-driven corrections | Published findings can be traced to fixes for evidence grading, approval handling, and preservation of unresolved findings. | [September 21 retrospective](evaluations/review-correction-traces-2026-09-21.md) |
| Review conditions surviving a session | A public PR implements a condition ten days after its originating review opened; the original transcript is operator-private. | [Carried condition and later repair](evaluations/review-correction-traces-2026-09-21.md#a-condition-survived-the-original-session-and-became-a-later-repair--pr-2317) |
| Sustained operation and recovery | The maintainer deployment records substantial ongoing traffic and exercised self-recovery paths. | [October 18 snapshot and queries](PRODUCTION_SNAPSHOT.md) |

Deployment counts measure recorded activity. Research questions on predictive
lift, the outcome effect of pausing, and cross-operator verification are tracked
under [evaluation boundaries](#evaluation-boundaries).

The [two-process walkthrough](guides/AGENT_SESSION_WALKTHROUGH.md) was captured
on 2026-10-08 from a fresh Docker quickstart install. Its task is illustrative;
the server responses are real. It demonstrates attribution, outcome binding,
closure, retrieval, and declared successor lineage in one complete sequence.

## Review findings that became fixes

A [2026-09-21 audit](evaluations/review-correction-traces-2026-09-21.md) fixed a
sample of 20 recently created, merged PRs before inspecting their review
findings. Eight contain a public finding-to-revision chain; two more contain an
author-recorded internal review with a matching revision. The corrections
include:

- **Preserving evidence grades during storage.** Review found that evidence
  acknowledged at one grade was stored at a stronger grade. The revision
  preserved the assigned grade and covered the database write path.
  [Finding and correction](evaluations/review-correction-traces-2026-09-21.md#stored-evidence-became-stronger-than-the-acknowledged-evidence--pr-2316)
- **Preventing a formatting repair from manufacturing approval.** Review found
  that repairing an unparseable rejection could produce a recorded approval.
  The revision refuses repaired approvals and adds regression coverage.
  [Finding and correction](evaluations/review-correction-traces-2026-09-21.md#a-formatting-repair-could-manufacture-approval--pr-2321)
- **Keeping unresolved findings visible.** Review found that a later clean
  result could erase an earlier open finding. The corrected gate retains the
  finding until it is addressed or explicitly disposed of.
  [Finding and correction](evaluations/review-correction-traces-2026-09-21.md#a-later-clean-result-could-erase-an-open-finding--pr-2318)

A separate PR implemented a condition from an earlier structured review **ten
days after that review opened**. The condition remained recoverable after the
original session ended. The public PR cites the review and contains the repair;
the originating review transcript is operator-private.
[Carried condition and patch](evaluations/review-correction-traces-2026-09-21.md#a-condition-survived-the-original-session-and-became-a-later-repair--pr-2317)

These records establish specific, consequential corrections and continuity of
review conditions. They come from a fixed sample of 20 recent merged PRs, with
no matched unreviewed or conventional-review control; some reviews were
author-relayed or filed under the same GitHub identity. Dialectic review,
councils, external models, and ordinary repository review have incomplete
common instrumentation, so this channel is one of several through which review
runs. A prospective comparison (`accountable-coordination-ablation-v0`) was
registered and closed on 2026-10-01 before any run; it needs more than one
operator.

<details>
<summary>Recorded exercise of the review constraint</summary>

The [historical exercise record](CHANGELOG.md) reports 33 non-canary sessions
with a submission after a reviewer's rejection. The
[September 17 review audit](plans/competitive-survival-audit-review-2026-09-17.md)
reports 81 non-canary verdicts since the 2026-07-02 reviewer-label split,
77 carrying reviewer conditions. Of 42 recorded dissents, one is a parse
failure recorded as disagreement rather than a reviewer's judgement. These
are retained historical counts, not a current fleet rate.

</details>

## Where the deployment numbers come from

The [2026-10-18 operational snapshot](PRODUCTION_SNAPSHOT.md) records sustained
maintainer traffic. Its captured JSON and read-only SQL give the counts,
event composition, and date ranges at one cutoff:

| Record | Count and scope |
|---|---|
| Audit and telemetry | **6,593,939 retained events**, including **52,264 in the preceding week**. Session-resolution observations and cross-device-call records make up 88.29% of the total. |
| State observations | **102,728 stored EISV rows** in `core.agent_state`. These are observations, not independent agents or trials. |
| Recovery | **20 recorded self-recoveries** among 27 non-automatic lifecycle-resume records in the snapshot’s broadcaster-form subset; direct fallback/test records without a `type` field are excluded. |
| Shared findings | **2,129 retained discovery records** in the knowledge graph. |
| Process registry | **8,677 retained process-instance records**; 287 distinct identities emitted audit events in the preceding week. |

The source is CIRWEL's own single-operator deployment, whose agents also help
build the system. The counts are retained inventories of recorded activity and
exercised paths across resident, ephemeral, development, and test/operational
traffic. The [deployment data notes](operations/DEPLOYMENT_DATA_CAVEAT.md)
explain the sampling and co-development context. The August snapshot remains
available as a historical record on the same page.

Separately, the published trajectory dataset contains **32,181 labeled
windows**: 20,655 overlapping real windows from one 39-day Raspberry Pi run and
11,526 synthetic windows. The [dataset card](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories)
documents windowing, generation, and per-row provenance; the
[evaluation catalog](EVALUATION_INDEX.md#labelled-sets) indexes the set. This
published dataset was not regenerated for the October operational inventory.

### What is built

The repository includes automated tests, migration checks, and documentation
checks alongside its callable interface. The following counts are **dated
structural snapshots**; current capabilities and setup are described in
[Capabilities and deployment](CAPABILITIES_AND_DEPLOYMENT.md).

<details>
<summary>Repository counts and counting methods</summary>

| Surface | Recorded size |
|---|---|
| Complete tool registry | **43 registered tools**, plus 8 workflow aliases; 8 routers over 52 actions and a 70-entry legacy alias table |
| Test suite | **18,193 test functions** across 965 files |
| Database migrations | **76** |
| Python modules | **567** across `src/`, `governance_core/`, and reference residents |
| Documents | **328** |
| Companion repositories | **7**, including the SDK, host adapter, Raspberry Pi testbed, and resident userland; see [Sibling repos](README.md#sibling-repos) |

The tool/router/alias counts were recorded 2026-09-20 at `76a68c02` from
`src/tool_meta.py` and router `ACTION_FIELDS` declarations. The test, migration,
module, and document counts were recorded 2026-10-18 from the committed tree at
`dd793928ad5ebc136a35815fbfd510c666f2e982`; the
[repository inventory](evaluations/production-snapshots/2026-10-18-repository.json)
records the counting methods. Test counts include synchronous and asynchronous
`test_*` definitions in tracked `tests/**/test_*.py` files, including nested
definitions; they are source counts, not collected or passing test cases.
Migration, module, and document counts use tracked paths in the stated tree.
Companion repositories are listed in the linked ecosystem overview.

The complete catalog is callable on every transport; the default initial
listing is progressive, with omitted names accessible through `use_tool` after
discovery.

</details>

## Outside review and acceptance

**Hermes Agent accepted the UNITARES lifecycle plugin into its plugin catalog**
on 2026-09-26, followed by version updates through 0.3.3 on 2026-10-06.
[Catalog acceptance](https://github.com/NousResearch/hermes-agent/commit/5c72a58543)
· [Recorded update](https://github.com/NousResearch/hermes-agent/commit/8d4e867b54)

The agent-side contract is also distributed as
[`unitares-sdk` on PyPI](https://pypi.org/project/unitares-sdk/), published from
CI through a trusted publisher. These records establish integration acceptance
and package availability. External adoption remains unvalidated.

### Security response

Two high-severity advisories published on 2026-10-05 document fixes in 3.2.0:

- **External report and coordinated disclosure:** EQSTLab and 2REBCat reported
  an arbitrary file write through path traversal in export filenames.
  [Advisory and fix](https://github.com/cirwel/unitares/security/advisories/GHSA-fgfh-wj4j-4xgj)
- **Maintainer-discovered vulnerability:** a trusted-network REST/WebSocket
  bypass was reachable from a browser page through CSRF or DNS rebinding.
  [Advisory and fix](https://github.com/cirwel/unitares/security/advisories/GHSA-mph4-qq2r-pc5v)

Two medium-severity advisories published on 2026-10-15 document fixes in 3.3.0.
Neither credits an outside finder:

- **Lifecycle actions on another agent:** any bound client could archive,
  delete or unpause another agent, and an agent could give itself the operator
  label. [Advisory and fix](https://github.com/cirwel/unitares/security/advisories/GHSA-r9q5-7j8h-82rr)
- **Identity from a UUID:** an agent's identity could be assumed from its UUID
  through the derived session id.
  [Advisory and fix](https://github.com/cirwel/unitares/security/advisories/GHSA-6fqf-5ccj-vcc4)

## Evaluation boundaries

### Predictive lift

The frozen 2026-08-09 outcome evaluation did not establish predictive lift:
no slice cleared the selection-aware null (selective p = 0.070–0.567). The
result is **inconclusive, not a demonstrated negative**. The first power
characterisation was withdrawn for corrupted synthetic pairing and uncontrolled
class-balance drift. The preserved record lacks the cluster geometry needed
to reconstruct read-specific power, so the corrected
[power audit](operations/falsifiability-power-audit-2026-08-23.md) sets no
standing AUC ceiling.

After the frozen cutoff, recurring automation exposed live discrimination
output: the ablation watchdog completed 42 of 51 executions and the dogfood
guard completed 43 of 52. Those jobs are paused. The
[2026-12-01 gate](proposals/registered/eisv-outcome-grounding-stop-rule-v0.md)
remains an operational decision rule, but it is not the only post-registration
read and cannot be described as clean single-read blinding; its report must
disclose the interim access and read-specific power.

The read was amended on 2026-10-15, before any read in its registered units had
run. It now admits only data recorded before `2026-10-15T13:45Z`, a boundary set
no later than the first deployment of a change to the live EISV update path; that
change deployed about 30 minutes afterwards. The
[stop rule's instrument-change record](proposals/registered/eisv-outcome-grounding-stop-rule-v0.md#amendment-2026-10-15-data-boundary-at-the-first-instrument-change)
documents both times. The 2026-12-01 result must therefore be described as **amended after
interim exposure**, not as an untouched confirmatory read. Fewer outcomes are
eligible as a result. The cluster threshold is not lowered, so a shortfall
closes the read as inconclusive, not as a measured null.

An operational `FAIL` can close a scheduled line of work without scientifically
refuting the underlying capability. The
[inference-status contract](ontology/falsification-inference-containment-2026-08-22.md)
sets the requirements for scientific refutation: target, counterfactual,
independent unit, power, decision rule, and read protocol.

### Pause delivery

Governed pauses are delivered. The audit log holds 370+ `lifecycle_paused`
events since April 2026, every one triggered by governance (energy-integrity
imbalance, high-risk verdicts, CIRS oscillation and risk-ceiling checks, low
basin) and none by an operator; 19 fell between 2026-08-09 and 2026-09-21, the
most recent pause on record. They span 35 distinct agent ids, one of which
accounts for 41% of the events (queried 2026-10-21; retention starts
2026-04-01, so earlier pauses are not counted). A 2026-08-06 audit found that a cadence window had downgraded 195
of 218 recorded pause decisions (89.4%) before delivery; the current rate has
not been re-measured. See
[ledger rows 24, 27, and 28](ontology/eisv-proprioception-contract.md).
Whether pausing changes outcomes is an open research question; answering it
takes a governed-versus-ungoverned comparison.

### Local control and cross-operator trust

Identity, telemetry, evidence, and policy history stay on infrastructure the
operator controls. Core storage has no required vendor-service dependency.
The current trust boundary is one operator and its explicitly trusted issuer.
Independent cross-operator verification, consensus, and enforcement remain
research goals.

Server-side Ed25519 lease attestations exist. Party-level HMAC signature minting
is retired; new dialectic resolutions are unsigned by design. A separate
server-countersigned resolution receipt is wired but dormant and has issued no
receipts as of the 2026-09-25 decision record. The
[federation trust decisions](proposals/active/federation-trust-decisions-2026-09-25.md)
and [receipt proposal](proposals/active/dialectic-resolution-receipt-v0.md)
preserve the signing history and conditions for further work. The
multi-principal trust track starts when an outside party with its own
administrative root asks to verify a deployment record, or with external
funding for, or an outside principal joining, the multi-principal testbed (the
decision record's 2026-10-19 addendum).

#### Identity binding and the lease plane

The Docker quickstart enforces process-bound proofs for `maintenance:/` leases;
other kinds remain staged until their producers carry proofs. Multi-issuer
trust requires lease principals to retain both issuer and subject, and active
leases must be drained before changing issuer. See the
[architecture reference](UNIFIED_ARCHITECTURE.md) for proof modes and signing
mechanics.

### Research and threat model

The [scope and threat model](SCOPE_AND_THREAT_MODEL.md#scope-and-threat-model)
owns this boundary: the deployed policy path versus the companion paper's
research formulation, behavior concealed or optimized outside the monitored
signals, and the independent evidence and controls that correctness, safety,
and resistance to a motivated attacker require.
