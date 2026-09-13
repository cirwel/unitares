# Evidence and limits, in full

The root README states the product promise and links here for the measured
record. This page carries the qualifications, provenance, deployment numbers,
and evaluation boundaries in full so the landing page can remain a concise
explanation of what UNITARES offers.

## Claim ledger

Every claim below carries an evidence class. A class says what a result
supports; it is not a positive or negative judgement about the project. A
registered operational `FAIL` can close a scheduled line of work without
scientifically refuting the underlying capability, and a claim earns `REFUTED`
only when target, counterfactual, independent unit, power, decision rule, and
read protocol all support it — see the
[inference-status contract](ontology/falsification-inference-containment-2026-08-22.md).

| Evidence class | What it licenses |
|---|---|
| **Operational observation** | A named mechanism ran in the stated deployment. Not benefit, correctness, or generality. |
| **Exercised path** | A code path ran and left countable, replayable records. Execution, not benefit. |
| **Non-detection** | The test did not separate the candidate from its comparison. Without power, that is not absence. |
| **Structural limit** | A boundary that follows from the design itself. More data does not move it. |
| **Untested** | No suitable measurement has been made. |

Three further classes — **Benchmark pass / fail**, **Unidentified /
inconclusive**, and **Mismatch / path bound** — are available for result types
this table does not currently hold.

### Current claim status

| Question | Status | What the record supports |
|---|---|---|
| Sustained operation | **Operational observation** | The maintainer deployment has run continuously under real load. The counts below are rows, events, and configured residents. |
| Identity and audit trail | **Exercised path** | Process-bound writes, evidence records, policy responses, and replayable audit history are deployed. This establishes mechanism execution. |
| Pause actuation and delivery | **Event reconciled; protection untested** | A governed pause landed on 2026-08-09. At the 2026-08-06 audit, a cadence window had downgraded 195 of 218 recorded pauses (89.4%) before delivery; the current rate has not been re-measured. See [ledger rows 24, 27, and 28](ontology/eisv-proprioception-contract.md). |
| Predictive lift | **Non-detection; inconclusive for weak effects** | In the frozen 2026-08-09 cohort, no slice cleared the selection-aware null (selective p = 0.070–0.567). The first power characterisation was withdrawn for corrupted synthetic pairing and uncontrolled class-balance drift, and the preserved record omits the cluster geometry needed to reconstruct read-specific power, so the corrected [power audit](operations/falsifiability-power-audit-2026-08-23.md) sets no standing AUC ceiling. |
| Incident prevention or benefit from pausing | **Untested** | No governed-versus-ungoverned comparison has shown an incident prevented or an outcome improved by pausing. |
| Review binds on the reviewed agent | **Exercised path** | A paused agent cannot resolve its own session over a standing reviewer objection: the submission is recorded, the session is not resolved, and it waits for facilitation (`src/dialectic_protocol.py`). 33 non-canary sessions carry such a submission after a reviewer's rejection. Since the 2026-07-02 reviewer-label split, 81 non-canary verdicts are recorded and 77 carry the reviewer's conditions; 42 dissent, one of which is a parse failure recorded as disagreement rather than a reviewer's judgement. |
| Benefit from review and coordination | **Untested** | These records cover dialectic-mediated review only: review run through subagent councils or external models leaves no row unless filed through `reviewer_provenance`, a field no caller has yet populated, so that gap is in recording rather than occurrence. Benefit is separate and unmeasured — every change went through the reviewed path, leaving zero untreated cases to compare against. |
| Robustness to a motivated attacker | **Structural limit** | A capable process can optimize or conceal behavior outside the monitored proxy; independent evidence remains necessary. See the [scope and threat model](SCOPE_AND_THREAT_MODEL.md). |

The outcome read carries a protocol qualification. After the frozen cutoff,
recurring automation exposed live discrimination output: the ablation watchdog
completed 42 of 51 executions and the dogfood guard completed 43 of 52. Those
jobs are paused. The fixed
[2026-12-01 gate](proposals/eisv-outcome-grounding-stop-rule-v0.md) remains
an operational decision rule, but it is not the only post-registration read and
cannot be described as clean single-read blinding; its report must disclose the
interim access and read-specific power.

The validation the system does claim — reliability, faithfulness under
intervention, and calibration — is scoped and partly built; the
[roadmap](../ROADMAP.md) tracks it. The DOI identifies a
[public preprint](https://doi.org/10.5281/zenodo.19647159), not peer-reviewed
validation.

## Where the deployment numbers come from

At the [2026-08-11 frozen snapshot](PRODUCTION_SNAPSHOT.md), the maintainer
deployment provided operational evidence that the system runs at length under
real load:

| Evidence | Scope |
|---|---|
| **4,573,890 audit/telemetry events** | Continuous maintainer-run operation since 2025-11-28, the first identity record. Session-resolution observations and cross-device-call records make up 91.4%. Measures infrastructure load and uptime. |
| **71,141 stored EISV state rows** | Longitudinal state observations in `core.agent_state`. The unit is the observation; the fleet producing them is the six residents below. |
| **15 recorded self-recovery events** | Of 21 canonical, non-automatic lifecycle-resume records. Shows the recovery path was exercised. |
| **32,181 labeled EISV windows** | [20,655 overlapping real windows from one 39-day Raspberry Pi run plus 11,526 synthetic windows](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories). Window parameters, the real/synthetic split (a per-row `provenance` column), and the generating pipeline are documented on the dataset card and indexed in the [evaluation catalog](EVALUATION_INDEX.md#labelled-sets). The unit is the window; every real window comes from the single Raspberry Pi run named above. |
| **6 long-running resident agents** | Configured and operating in the maintainer deployment at the snapshot date; one runs on separate hardware, the same Raspberry Pi that produced the labeled-window dataset. The same single-operator fleet as every number above. |

The maintainer deployment is **single-operator and co-development dogfood**: most
agents governed by the system are also building the system. Read
[`DEPLOYMENT_DATA_CAVEAT.md`](operations/DEPLOYMENT_DATA_CAVEAT.md) before citing
a fleet number.

## What is built

One operator, since 2025-11-20. The counts below are structural facts about this
repository and its companions, not a claim that any of it outperforms an
alternative. They answer one question an evaluator reasonably asks first: is this
a prototype or a system?

| | |
|---|---|
| **42 tools** on the wire | one complete catalog, every name advertised on every transport (legacy `GOVERNANCE_TOOL_MODE` settings are accepted and ignored); 8 of them are consolidated routers over 52 actions, 8 workflow aliases carry the agent-facing names, and a 70-entry alias table resolves legacy names |
| **13,614 test functions** | across 776 files, sharded in CI, with the fleet-neutrality and evidence contracts enforced as tests rather than as conventions |
| **67 database migrations** | slot-and-name drift is gated by the repo doctor |
| **524 Python modules** | `src/`, `governance_core/`, and the reference residents |
| **226 documents** | ontology, proposals, operations runbooks, and the evaluation index, with dead-reference checks in CI |
| **7 companion repositories** | including a published SDK, a host adapter, a Raspberry Pi testbed, and the resident userland; the main integrations are listed under [Ecosystem](../README.md#ecosystem) |

Recounted 2026-09-09 against tracked files at `e017c45e`, each with the command
that produced it. Test files: `git ls-files tests/` filtered to `test_*.py`;
test functions: an AST walk of those files for `def test_*`. Migrations:
`git ls-files db/postgres/migrations/ | wc -l`. Python modules:
`git ls-files src/ governance_core/ agents/ | grep -c '\.py$'`. Documents:
`git ls-files docs/ | grep -c '\.md$'`. The tool, router, action, and alias
figures are read from `src/tool_meta.py` and the routers' `ACTION_FIELDS`
declarations, not from a directory count; that module carries one record per
dispatch tool plus the eight workflow aliases, so the tool figure is its
non-alias records.

## Local control and future federation

Identity, telemetry, evidence, and policy history stay on infrastructure the
operator controls, with no outbound dependency on a vendor service.

The architecture exposes several of the seams a later federation experiment
would need: process-bound identity, evidence provenance, a
[versioned telemetry envelope](ontology/eisv-telemetry-envelope-v1.md), and
policy decisions with named reasons.

**The blocker is named, not unknown.** Resolution attestations are HMAC keyed on
each agent's api_key. That is symmetric: a verifier needs the signing key, and
holding it would also let them forge a signature. Sound for its deployed purpose
of one operator attesting inside their own trust boundary, and explicitly not
non-repudiation. Asymmetric or DPoP-style keys were considered and shelved on
2026-04-19, so until that is revisited a record from this system cannot be
verified by an operator who does not already trust its issuer, which is the whole
problem a federation exchange has to solve. Whether the remaining records suffice
to exchange cross-operator attestations without centralizing raw telemetry is
open on the **multi-principal trust** track in the [roadmap](../ROADMAP.md).

## Identity binding and the lease plane

Identity binding is what makes stored findings, reviews, and leases attributable
to a process rather than to a display label. The Docker quickstart enforces this
for `maintenance:/` leases; other kinds remain staged until all of their
producers carry proofs. Governance keeps the continuity credential and private
signing key; the lease plane verifies a short-lived token bound to a
deployment-specific audience plus the exact method, path, and request-body hash,
then consumes its nonce once. This version accepts one explicitly trusted issuer.
Multi-issuer federation remains blocked until lease principals persist both
issuer and subject; active leases must be drained before changing issuer.
`legacy`, `hybrid`, and `attestation` proof modes support staged upgrades. The
lease plane listens on `http://127.0.0.1:8788` with bearer auth.
