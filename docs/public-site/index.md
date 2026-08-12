# Runtime accountability for long-lived AI agents

UNITARES is a self-hosted MCP and HTTP service that gives long-running agent
processes accountable identities, evidence-linked check-ins, and policy responses
with named reasons. It complements evals, guardrails, and sandboxes; it does not
replace them.

**Current public releases:** [server v2.17.0](https://github.com/cirwel/unitares/releases/tag/v2.17.0)
· [Python SDK 0.1.0](https://pypi.org/project/unitares-sdk/)
· [multi-architecture container](https://github.com/cirwel/unitares/pkgs/container/unitares)
· Apache-2.0

## The operating problem

An agent can remain within per-action permissions while becoming poorly
calibrated over a long task. UNITARES retains the longitudinal record that an
action-level guardrail usually does not: what a process claimed, what evidence
was available, what state the service estimated, and which policy response it
returned.

## What operators get

| Surface | Operational value |
|---|---|
| Accountable identity | Bind writes to a process instance and retain lineage across explicit handoffs. |
| Evidence-linked calibration | Compare stated confidence with tests, exit codes, tool results, review labels, and recorded outcomes. |
| Policy and recovery | Return a named action, reason, and next step; support governed pause and review paths. |
| Operator visibility | Inspect lifecycle, state, evidence, and decision history through MCP, HTTP, and a self-hosted dashboard. |

The deployed policy path uses auditable behavioral state estimation. The
information-theoretic and ODE formulation remains a research target and parallel
diagnostic path, not the live decision mechanism.

## Try the released surfaces

Run the documented stack and a six-check-in wiring demo:

```bash
git clone --branch v2.17.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make demo
```

Install the resident-agent SDK from PyPI:

```bash
python -m pip install unitares-sdk==0.1.0
```

Or inspect the signed multi-architecture server image:

```bash
docker pull ghcr.io/cirwel/unitares:v2.17.0
```

The demo establishes that the stack is wired. It does not establish predictive
value or governance efficacy.

## Evaluate the project

| Question | Evidence path |
|---|---|
| What is live, proposed, or falsifiable? | [Reviewer Guide](https://github.com/cirwel/unitares/blob/master/docs/REVIEWER_GUIDE.md) |
| What does the runtime compute? | [Computation reference](https://github.com/cirwel/unitares/blob/master/docs/EISV_COMPUTATION.md) |
| What are the threat model and blind spots? | [Scope and threat model](https://github.com/cirwel/unitares/blob/master/docs/SCOPE_AND_THREAT_MODEL.md) |
| What evidence can be regenerated? | [Evaluation catalog](https://github.com/cirwel/unitares/blob/master/docs/EVALUATION_INDEX.md) |
| How is the system operated and released? | [Operations docs](https://github.com/cirwel/unitares/tree/master/docs/operations) |
| What remains on the roadmap? | [Roadmap](https://github.com/cirwel/unitares/blob/master/ROADMAP.md) |

## Evidence boundary

The public operational record comes from one long-running maintainer deployment.
External adoption remains unvalidated, and the frozen outcome-lift evaluation
found no result beyond a selection-aware null. UNITARES does not claim to prove
correctness, ethics, safety, or incident prevention. It makes process identity,
telemetry, evidence, and policy history inspectable so those claims can be tested
rather than assumed.

## Federation direction

Today, each deployment is governed by its own operator. The architecture exposes
versioned telemetry, provenance, identity, and named policy decisions so future
work can test cross-operator attestations without centralizing raw telemetry.
Cross-governor trust, consensus, and enforcement are research goals, not deployed
guarantees.
