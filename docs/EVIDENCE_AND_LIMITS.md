# Evidence and limits, in full

The [claim ledger lives on the README](../README.md#evidence-and-limits): every
public claim, its evidence class, and what that class licenses stay on the
landing page rather than behind a link. This page carries the supporting record
the ledger is drawn from — where the deployment numbers come from and what they
measure, the structural build record, and the two boundaries that need more room
than a table row gives them.

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
| **42 tools** on the wire | fourteen advertised by default and the rest behind `GOVERNANCE_TOOL_MODE`; 8 of them are consolidated routers over 52 actions, 8 workflow aliases carry the agent-facing names, and a 70-entry alias table resolves legacy names |
| **12,619 test functions** | across 720 files, sharded in CI, with the fleet-neutrality and evidence contracts enforced as tests rather than as conventions |
| **64 database migrations** | slot-and-name drift is gated by the repo doctor |
| **509 Python modules** | `src/`, `governance_core/`, and the reference residents |
| **208 documents** | ontology, proposals, operations runbooks, and the evaluation index, with dead-reference checks in CI |
| **7 companion repositories** | listed under [Ecosystem repositories](../README.md#ecosystem-repositories), including a published SDK, a host adapter, a Raspberry Pi testbed, and the resident userland |

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
