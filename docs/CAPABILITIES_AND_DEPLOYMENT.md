# Capabilities and deployment

UNITARES is a self-hosted, single-operator federation kernel: many independent
agent runtimes and harnesses share one operator-controlled server and authority
domain. The core workflow is identity, claims and evidence, review, outcomes,
and reconstruction.

## Start with the core workflow

| Task | Public entry points | What the caller must supply or retain |
|---|---|---|
| Bind identity | `start_session(force_new=true)`, `identity` for inspection or explicit same-process rebind | Retain `client_session_id`. A fresh process gets a fresh identity; declare lineage only for a real causal relationship. |
| Record claims and evidence | `sync_state`, `store_finding`, `update_finding` | `sync_state` submits a work report from which durable state is derived, and each check-in keeps a bounded excerpt of its report text; durable claim text belongs in a finding. Search before storing. Confidence is optional, and supplying it can mint a prediction. |
| Review | `request_review`, `dialectic` | Preserve the session ID, positions, reviewer judgment, and unresolved conditions. `consult` answers are advisory records, kept apart from governed review verdicts. |
| Record outcomes | `record_result` | Record the observed outcome and provenance; use the returned prediction ID to grade the intended prediction. |
| Reconstruct work | `search_shared_memory`, `knowledge` reads, `dialectic` reads, `export`, operator-gated `observe(action="outcome_evidence")` | Retrieve relevant records and distinguish current claims, superseded claims, disagreement, and missing evidence. These reads have different authorization and retention boundaries. |

Reconstruction is a client workflow over these records and operations. Each `sync_state` check-in keeps a bounded excerpt of its report text
(500 characters by default, `UNITARES_CHECKIN_EXCERPT_CHARS`; 0 turns it off),
not the full text, and exported governance history does not include shared
knowledge. Preserve source IDs and gaps when summarizing.
Comparative reconstruction fidelity and correction propagation remain
evaluation questions.

Use `describe_tool(tool_name=..., action=...)` for the full schema of the action
you need. Primary workflow names and canonical implementations are both public;
see the [interface contract](INTERFACE_CONTRACT.md) for the authoritative mapping.

## Additional capabilities

| Capability | Entry points | Prerequisites and limits |
|---|---|---|
| Behavioral state and policy | `sync_state`, `check_working_state`, `self_recovery` | State estimates and policy actions come from the deployed heuristic instrument ([EISV computation](EISV_COMPUTATION.md)). A host integration enforces actions beyond governed writes. |
| Advisory inference | `consult` | Needs a configured provider/host and appropriate privacy/authorization. Cloud-enabled processing may send submitted content externally. No bundled model is implied. |
| Observation and calibration | `observe`, `calibration` | Built from retained observations and outcomes. `calibration` `update`, `backfill` and `rebuild` write calibration state; `check` reads it. |
| Operations and configuration | `agent`, `config`, `admin`, `health_check` | Action-specific authorization applies. `health_check` returns the latest background probe snapshot; read `_cache.age_seconds` and `_cache.stale` before treating it as current. |
| History and specialist knowledge | `export`, `knowledge` | Export, retention, graph, and backend-specific actions have their own parameters and prerequisites. |
| Signed coordination | Deployment's lease-plane interface | Needs the configured lease service and identity proofs. A lease covers the surface it names. |

“Core” and “additional” are reader guidance. They do not rename the existing
`essential`, `common`, and `advanced` tiers. The complete mounted public catalog
remains negotiable and callable, while the initial transport listing is
progressive by default. Use `list_tools(lite=true)` for every capability name,
`describe_tool` for its parameters, and `use_tool` when its schema was omitted
from the initial listing. `UNITARES_TOOL_ADVERTISEMENT=full` restores the full
up-front listing. Category, tier, and `essential_only` filters affect browsing,
not target authorization.

## Choose a deployment profile

| Profile | Supplied services | What the operator supplies or verifies |
|---|---|---|
| Local single-operator Compose | PostgreSQL with AGE/pgvector, Redis, lease plane, HTTP/MCP server | Client integration, inference providers, and participating reviewers. Use the verified release in the [quickstart](../README.md#install). |
| Private provider-hosted Glama bundle | Private PostgreSQL with AGE/pgvector, Redis, lease plane and HTTP runtime behind stdio; persistent `/data` | Glama runs the compute and stores the volume; the operator controls account configuration and credentials. Persistence, resource limits, and deployment validation must be verified. No model, automated reviewer fleet, or resident fleet is bundled. See [Glama installation](deployment/glama.md). |
| Client connected to an existing server | MCP/HTTP access to the operator's deployment | Server provisioning, available integrations, access policy, and lifecycle hooks are the operator's responsibility. |
| Bare process / incomplete dependencies | May initialize MCP and advertise tools | Unsupported for durable use: storage-backed calls need PostgreSQL and Redis. |

Full-text knowledge search is the default in Compose and the Glama bundle
(semantic search requires `docker-compose.full.yml`). For the knowledge backend
(`UNITARES_KNOWLEDGE_BACKEND`), Compose defaults to `age`: AGE enables
graph-specific behavior, including replacement-link and response-chain
traversal. The Glama bundle and unset source installs default to `postgres`,
which marks an older finding superseded but cannot retain the successor edge.
That backend choice is distinct from discovery tiers.
Redis carries live session bindings; without it the server runs in a degraded
local-only mode.

## Diagnose discovery separately from execution

Record the source commit or release, transport, interface contract version and
surface hash, installed plugins, build command, and dependency health before
comparing two catalogs. A directory's cached tool count is not a product tier.
Compare names and schemas, then exercise representative identity, storage,
retrieval, outcome, and review operations with test data in an authorized test
deployment. Check inference and peer review against their configured services.
A `tools/list` result or a review request shows discovery; an exercised call
shows execution.
