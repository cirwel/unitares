# UNITARES Reviewer Guide

**Created:** May 23, 2026

**Last Updated:** October 18, 2026

**Status:** Active

UNITARES is accountability infrastructure for long-running AI agents: a
self-hosted, single-operator server that keeps attributed records of identity,
claims and evidence, review, outcomes, and reconstruction across sessions,
handoffs, and agents from different vendors.

Start with the captured workflow and concrete corrections below, then exercise
that workflow in a test deployment. The [evidence and limitations](EVIDENCE_AND_LIMITS.md)
page collects the results and their scope; [capabilities and deployment](CAPABILITIES_AND_DEPLOYMENT.md)
covers interfaces, storage, and authorization.

## Evidence to check

| What to evaluate | Record to inspect | What it establishes |
|---|---|---|
| Attribution and outcome binding | [Process A in the captured walkthrough](guides/AGENT_SESSION_WALKTHROUGH.md#process-a) | Calls bind to one process; its finding retains authorship; an outcome grades the intended prediction and retains its evidence grade. |
| Continuity across processes | [Process B in the walkthrough](guides/AGENT_SESSION_WALKTHROUGH.md#process-b) | A fresh process retrieves A's resolved finding, author, and closure class, then declares the handoff. |
| Review findings becoming corrections | [September 21 review retrospective](evaluations/review-correction-traces-2026-09-21.md) | Public finding-to-revision chains preserve evidence grades, prevent formatting repair from manufacturing approval, and keep unresolved findings visible. |
| Review conditions surviving a session | [Carried condition and later repair](evaluations/review-correction-traces-2026-09-21.md#a-condition-survived-the-original-session-and-became-a-later-repair--pr-2317) | A public PR implements a condition ten days after its originating review opened; the original transcript is operator-private. |
| Operation and recovery | [Dated production snapshot and queries](PRODUCTION_SNAPSHOT.md) | Recorded deployment activity and exercised recovery paths, with counting methods and scope. |

The walkthrough's task is illustrative; its server responses were captured
from a fresh Docker install on 2026-10-08. The retrospective establishes
specific corrections in a recent, non-random sample. Comparative benefit over
other review or handoff approaches remains unmeasured. Deployment counts
measure recorded activity, not uptime or independent trials.

## Verify one complete workflow

Use the [walkthrough](guides/AGENT_SESSION_WALKTHROUGH.md) for exact calls and
responses. The same sequence is a small first integration for coding, research,
operations, or monitoring agents:

1. **Search before repeating work.** Use `search_shared_memory` and read relevant
   findings, including their status and corrections. Authorized reads can precede
   identity binding.
2. **Bind the process.** Call `start_session(force_new=true)`, retain
   `client_session_id`, and thread it on later calls. Confirm that
   `check_working_state` returns the new process's `agent_uuid`.
3. **Preserve the finding.** Use `store_finding` for a durable claim and its
   evidence. Use `sync_state` after meaningful work; its report text is transient,
   so it does not replace the finding. Retain a returned `prediction_id` when
   grading that check-in later.
4. **Request review when needed.** `request_review` starts a governed review;
   retain its session ID, positions, reviewer judgment, and unresolved conditions.
   Inspect the completed exchange with `dialectic` reads. A request alone does
   not establish completed review, and an advisory `consult` is a separate path.
5. **Record what happened.** Send `record_result` with outcome provenance and the
   intended `prediction_id`. The walkthrough shows both binding and the
   `self_report_with_refs` grade: an agent-reported test result remains a
   self-report. Ordinary test failures are task-negative evidence unless a
   separate contract, authority, or harm boundary was crossed.
6. **Close and retrieve.** Use `update_finding` with the closure class and evidence.
   Release process presence when exiting. A successor searches for the record,
   starts fresh, and declares `parent_agent_id` with `spawn_reason="explicit"`
   only for a real handoff from that finished predecessor.

The captured walkthrough covers steps 1–3 and 5–6. For step 4, inspect the
[review protocol](../src/dialectic_protocol.py) and the correction records above,
then exercise review with participating reviewers in your test deployment. A
paused agent cannot resolve its own review over a standing reviewer objection;
the objection remains pending for facilitation.

Evaluate the record that a later reader can actually retrieve: author, claim,
evidence grade, review conditions, outcome binding, closure, and any missing
links. Keep source IDs in a reconstruction. Updating a finding does not rewrite
copies already held in a handoff file or another agent's context; readers must
retrieve current records. Governance-history exports omit shared knowledge,
and retention, authorization, and replacement-link traversal have
[backend-specific boundaries](CAPABILITIES_AND_DEPLOYMENT.md#choose-a-deployment-profile).

## Implementation status: what's live vs proposed

These core mechanisms are shipped. Use the linked implementation and the
[canonical source registry](dev/CANONICAL_SOURCES.md) to verify behavior;
a proposal alone does not establish shipment.

| Mechanism | Public entry points | Where to verify |
|---|---|---|
| Process identity and declared lineage | `start_session`, `identity` | [`identity/`](../src/mcp_handlers/identity/), [identity contract](ontology/identity.md) |
| Durable findings and retrieval | `store_finding`, `update_finding`, `search_shared_memory`, `knowledge` reads | [`knowledge/handlers.py`](../src/mcp_handlers/knowledge/handlers.py), [knowledge schema](../db/postgres/knowledge_schema.sql) |
| Governed review | `request_review`, `dialectic` | [Review handlers](../src/mcp_handlers/dialectic/handlers.py), [protocol and resolution constraints](../src/dialectic_protocol.py) |
| Outcomes and prediction binding | `record_result` | [Outcome handler](../src/mcp_handlers/observability/outcome_events.py), [captured binding](guides/AGENT_SESSION_WALKTHROUGH.md#6-grade-the-check-in-against-what-happened) |
| Reconstruction | Knowledge and review reads, `export`, operator-gated `observe(action="outcome_evidence")` | [Capabilities and read boundaries](CAPABILITIES_AND_DEPLOYMENT.md#start-with-the-core-workflow), [export implementation](../src/mcp_handlers/introspection/export.py) |
| Behavioral state and policy | `sync_state`, `check_working_state`, `self_recovery` | [Deployed EISV computation](EISV_COMPUTATION.md), [architecture](UNIFIED_ARCHITECTURE.md) |

The [interface contract](INTERFACE_CONTRACT.md) maps workflow names to canonical
tools. Discover names with `list_tools(lite=true)` and schemas with
`describe_tool`; use `use_tool` for tools omitted from the initial progressive
listing. Legacy aliases have transport-specific support. PostgreSQL full-text
search is the default; AGE provides replacement-link and response-chain
traversal. Neither a tool listing nor a successful review request proves a
complete deployment.

## Fast path: three minutes

```bash
v=$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION)
git clone --branch "v$v" --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make demo
```

This installs the latest verified release. Cloning `master` instead gets
unreleased work. `make demo` is an **installation smoke test**: a fresh identity,
six check-ins, response shape, and warmup position. It stays below the
self-relative baseline threshold; use the workflow above to evaluate the
accountability record and the registered evidence below for prediction claims.

If a local UNITARES service already uses port `8767`, run `make demo` against it,
or start a separate stack on alternate host ports:

```bash
GOVERNANCE_HOST_PORT=18767 LEASE_PLANE_HOST_PORT=18788 docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
```

PostgreSQL and Redis are not published to host ports by default. Deployment
profiles, dependencies, and configured reviewer/provider requirements are in
[Capabilities and deployment](CAPABILITIES_AND_DEPLOYMENT.md).

## Research and operational boundaries

EISV computes weighted, smoothed runtime observations and controller history.
The [computation reference](EISV_COMPUTATION.md) defines the formulas, provenance,
and distinction between deployed indices and the paper's research targets.
`proceed` and `guide` are advisory; `pause` holds governed writes until recovery,
review, an operator, or expiry lifts it. External enforcement needs host
integration. The operator adopted moving EISV into an optional module on
2026-10-10; the [staged implementation plan](proposals/active/eisv-core-boundary-v0.md)
governs that work. The estimator still ships in the core path.

Correctness, safety, adversarial resistance, predictive lift, and incident
prevention require their own evidence. The [scope and threat model](SCOPE_AND_THREAT_MODEL.md)
and [evaluation boundaries](EVIDENCE_AND_LIMITS.md#evaluation-boundaries) state
what remains open. The current trust boundary is one operator and its explicitly
trusted issuer; independent cross-operator verification remains a research goal.

For deeper review, use the [evaluation index](EVALUATION_INDEX.md), public
[paper](https://doi.org/10.5281/zenodo.19647159),
[reproducibility kit](https://github.com/CIRWEL/unitares-repro-v6),
[embodied testbed](https://github.com/CIRWEL/anima-mcp), and
[trajectory dataset and benchmarks](https://github.com/CIRWEL/eisv-lumen).
Repository [CI](https://github.com/cirwel/unitares/actions),
[security policy](../.github/SECURITY.md), and
[contribution guide](../.github/CONTRIBUTING.md) document engineering checks.

## Falsifiability: inspect the registered evidence

Inspect the frozen records and run synthetic controls; **do not query live
outcome discrimination between registered reads**. The ablation harness compares
EISV/prior-state features with `previous_outcome_bad` on ranking (AUC) and
calibration (Brier). Its `UNASSESSED`, `NON_DETECTION`, and `SIGNAL_CANDIDATE`
classes use a selection-aware null; none alone is a project verdict.

**Without a deployment database**, exercise the pipeline with an injected bad
class. These are plumbing/containment checks, not prediction evidence:

```bash
python3 scripts/analysis/ablation_negative_controls.py --count 12
python3 -m pytest tests/test_ablation_negative_controls.py \
  tests/test_eisv_ablation_matrix.py tests/test_eisv_skeptic_report.py \
  tests/test_outcome_inventory.py
```

**With deployment data or the reproducibility kit**, first read the
[outcome-grounding stop rule](proposals/registered/eisv-outcome-grounding-stop-rule-v0.md)
and [design audit](ontology/falsification-design-system-audit-2026-08-23.md).
Between registered reads, only the support inventory is permitted here; it does
not expose discrimination:

```bash
export GOVERNANCE_DATABASE_URL=postgresql://...
python3 scripts/analysis/outcome_inventory.py --window-days 90 --leads 0,5,30
```

The matrix command and its 12 overall rows are preserved in the
[frozen 2026-08-09 read](operations/eisv-ablation-frozen-2026-08-09.md).
Running that command again still exposes outcome discrimination even with the
same cutoff; it is not reviewer hygiene. Use synthetic unit tests for the
lane/exclusion contract. The fixed 2026-12-01 decision command belongs only to
the registered execution, with all interim-access disclosures. Its October 15
amendment limits eligible data to before `2026-10-15T13:45Z`; the scheduled date
and cluster threshold remain unchanged. Describe that read as **amended after
interim exposure**, not as clean single-read blinding. A support shortfall closes
it as inconclusive, not as a measured null.

### What the frozen read establishes

At `2026-08-09T20:00:00Z`, with trusted anchors and the BEAM harness lane
excluded, the strict/task slices contain 223–227 outcomes, 53 bad rows, 28–29
prior-state permutation blocks, and 16 agents. Report rows, blocks, and agents
together; blocks are not independent failures.

All 12 overall scope/window/lead slices are `NON_DETECTION`: selective p ranges
from 0.070 to 0.567 after best-candidate selection is included in the null.
Several unadjusted estimates improve ranking and calibration, usually through
`prior_risk`, `prior_s`, or dispersion; those estimates do not establish the full
E/I/S/V decomposition. The [corrected power audit](operations/falsifiability-power-audit-2026-08-23.md)
withdraws its original table: preserved evidence cannot establish this read's
power. Cite the p-values with that limitation. A corrected synthetic procedure
supplies no valid historical power number for this cohort.

The result is a dated descriptive non-detection. It cannot distinguish absent
association from an underpowered read, validate EISV or its ontology, set a
standing AUC bound, establish prevention, or consume the December read.
An operational stop and scientific refutation have distinct requirements under
the [inference-status contract](ontology/falsification-inference-containment-2026-08-22.md).

### Preserve outcome and telemetry provenance

For CI/test producers using operator-gated `/v1/harness/outcome`, send confidence
only when the producer holds one. The endpoint ignores `prediction_id` because
an operator-asserted identity does not establish work correlation. An agent
instead uses `record_result` with its own bound session and prediction ID.
When confidence is absent, the server scrapes one and stamps
`calibration_excluded` with reasons; inventing confidence defeats that safeguard.

Since 2026-09-02, non-protocol instruments use the `corrected` fixture rule,
retaining rows excluded only for scraped confidence. This caused a counting
step, not event accrual: compare reports with `fixture_rule` held fixed.
`--fixture-rule registered` reproduces earlier counting. Protocol-bound reads
use their manifest's rule; coherence-shadow v0 stays on the registered rule.
The [decision packet](proposals/archive/outcome-fixture-conflation-decision-packet-v0.md)
documents `validation_visibility`, `calibration_excluded_only`, and
`scraped_only_rows_kept`.

Telemetry is future-only: pre-envelope rows remain `legacy/no-envelope`,
including the entire frozen read; outcomes without a joinable prior state are
`no_prior_state`. Never infer sources or backfill legacy coverage. Enforcement
slices are intervention-conditioned: they audit requested versus applied
behavior, not whether EISV caused or prevented an outcome. Health risk bins are
five-minute lead-separated, strict-external, controlled-fixture-excluded, and clustered by
prior measurement; sparse bins remain `sparse`/`inconclusive`. Different verdict,
experience-summary, and health-status bands answer different questions;
contract violations concern disagreement within the same persisted observation.
See the [telemetry contract](ontology/eisv-telemetry-envelope-v1.md).

## Deployment scope and next validation step

The maintainer deployment supplies operational evidence in a single-operator
setting. For efficacy research, report effective independent clusters, collapse
replicate identities, and prefer outcomes the acting agent neither authors nor
observes. The 2026-07-30 individuality read had effective n=4 despite nominal
n=7 because three identities shared one replicated Raspberry Pi. Independent
operators can supply new draws and are required for a multi-principal trust
claim; headcount alone does not establish independent evidence.
