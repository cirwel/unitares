# Capabilities and deployment

UNITARES is a self-hosted federation kernel: a shared accountability record for
agents that keep their own runtimes. The core workflow is identity, claims and
evidence, review, outcomes, and reconstruction. This guide organizes existing
capabilities; it does not add a schema, change a default, or narrow discovery.

## Start with the core workflow

| Task | Public entry points | What the caller must supply or retain |
|---|---|---|
| Bind identity | `start_session(force_new=true)`, `identity` for inspection or explicit same-process rebind | Retain `client_session_id`. A fresh process gets a fresh identity; declare lineage only for a real causal relationship. |
| Record claims and evidence | `sync_state`, `store_finding`, `update_finding` | Report work and evidence accurately. Search before storing; preserve correction/supersession links. Confidence is optional, and supplying it can mint a prediction. |
| Review | `request_review`, `dialectic` | Preserve the session ID, positions, reviewer judgment, and unresolved conditions. An advisory `consult` answer is not a governed review verdict. |
| Record outcomes | `record_result` | Record the observed outcome and provenance; use the returned prediction ID to grade the intended prediction. |
| Reconstruct work | `search_shared_memory`, `knowledge` reads, `dialectic` reads, `export` | Retrieve relevant records and distinguish current claims, superseded claims, disagreement, and missing evidence. Exported governance history does not include shared knowledge; retrieve that separately. |

These are existing records and operations, not a unified Claim/Evidence object
API. Reconstruction is a client workflow over them, not a single RPC or a
promise that all prior context is recoverable. Preserve source IDs and gaps
when summarizing. Comparative reconstruction fidelity, correction propagation,
and benefit over Git plus structured handoff remain evaluation questions.

Use `describe_tool(tool_name=..., action=...)` for the full schema of the action
you need. Primary workflow names and canonical implementations are both public;
see the [interface contract](INTERFACE_CONTRACT.md) for the authoritative mapping.

## Additional capabilities

| Capability | Entry points | Prerequisites and limits |
|---|---|---|
| Behavioral state and policy | `sync_state`, `check_working_state`, `self_recovery` | State estimates and named policy actions are deployed heuristics. Enforcement outside governed writes requires host integration. |
| Advisory inference | `consult` | Needs a configured provider/host and appropriate privacy/authorization. Cloud-enabled processing may send submitted content externally. No bundled model is implied. |
| Observation and calibration | `observe`, `calibration` | Depends on retained observations and outcomes. A score alone is not proof of correctness or prevention. |
| Operations and configuration | `agent`, `config`, `admin`, `health_check` | Action-specific authorization applies. Readiness must be checked in the actual deployment. |
| History and specialist knowledge | `export`, `knowledge` | Export, retention, graph, and backend-specific actions have their own parameters and prerequisites. |
| Signed coordination | Deployment's lease-plane interface | Needs the configured lease service and identity proofs; it is not automatic ownership of arbitrary host actions. |

“Core” and “additional” are reader guidance. They do not rename the existing
`essential`, `common`, and `advanced` tiers or introduce a reduced tool mode.
The complete registered catalog remains advertised. `list_tools(category=...)`
filters by topic and `list_tools(essential_only=true)` selects the essential
browsing subset; `describe_tool` supplies full parameter details after
abbreviated discovery descriptions.

## Choose a deployment profile

| Profile | Supplied services | What remains external or unproven |
|---|---|---|
| Local single-operator Compose | PostgreSQL with AGE/pgvector, Redis, lease plane, HTTP/MCP server | Client integration, inference providers, and participating reviewers. Use the verified release in the [quickstart](../README.md#quickstart). |
| Private Glama storage bundle | Private PostgreSQL with AGE/pgvector, Redis, lease plane and HTTP runtime behind stdio; persistent `/data` | Glama account persistence, resource limits, and deployment validation must be verified. No model, automated reviewer fleet, or resident fleet is bundled. See [Glama installation](deployment/glama.md). |
| Client connected to an existing server | MCP/HTTP access to the operator's deployment | Server provisioning, available integrations, access policy, and lifecycle hooks are the operator's responsibility. |
| Bare process / incomplete dependencies | May initialize MCP and advertise tools | Not a supported durable core installation. Discovery success does not make storage-backed calls usable. |

PostgreSQL full-text knowledge search is the default in the Glama bundle; AGE
selects graph-specific knowledge behavior. That backend choice is distinct from
discovery tiers. Redis carries live session bindings; local fallback is degraded
operation, not an equivalent supported production profile.

## Diagnose discovery separately from execution

Record the source commit or release, transport, interface contract version and
surface hash, installed plugins, build command, and dependency health before
comparing two catalogs. A directory's cached tool count is not a product tier.
Compare names and schemas, then exercise representative identity, storage,
retrieval, outcome, and review operations with test data in an authorized test
deployment. Inference and completed peer review need separate checks with their
configured services. A successful `tools/list` or review request alone is not an
end-to-end validation receipt.
