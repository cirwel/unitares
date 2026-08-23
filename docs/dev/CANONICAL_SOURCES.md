# Canonical Sources

**Last Updated:** 2026-08-17 (re-verified: all listed runtime sources and thin-doc targets resolve)

Use this page to resolve architecture disputes and doc drift.

Status: canonical authority map for active docs.

## Trust Order

When prose and code disagree, use this order:

1. Runtime code that computes or returns the behavior
2. Prose docs explicitly marked live or canonical
3. Historical docs and archived analyses

This ordering exists because the codebase has accumulated multiple eras of explanation. Some older docs accurately describe earlier phases of the system but no longer describe the runtime that agents interact with today. If an agent reads archived prose before it reads runtime code, it can form a coherent but outdated model of the system. This page exists to prevent that failure mode and give both humans and agents a compact rule for resolving contradictions without guesswork.

## Contested Claims Registry

Facts that were corrected once and must not silently revert. `scripts/diagnostics/check_doc_health.py`
(`check_contested_claims`) warns when the stale wording reappears on a reader-facing surface.
When you correct an architecture fact in prose, add a row here + a deny-pattern there,
and grep all reader-facing docs for the old claim **in the same PR**.

| Claim | Canonical wording | Owner |
|-------|-------------------|-------|
| Redis posture | Redis is the de-facto primary session store; the server boots without it in a degraded local-only mode (fine for the demo; sessions won't persist). It is not "optional" in production. | `docs/UNIFIED_ARCHITECTURE.md` · `docs/proposals/redis-retirement-v0.md` |
| Database bootstrap | The server refuses an uninitialized database. Docker initializes an empty volume through `db/postgres/docker-initdb.sh`; advanced bare-metal installs and resets run `scripts/install/bootstrap_postgres.sh --apply` before restart. | `src/db/postgres_backend.py` · `db/postgres/docker-initdb.sh` · `scripts/install/bootstrap_postgres.sh` |
| SDK publication | `unitares-sdk` 0.1.0 is published on PyPI. Use the matching Git tag only for deliberate unreleased-source testing. | `agents/sdk/README.md` · `COMPATIBILITY.md` |
| REST tool-call envelope | `POST /v1/tools/call` accepts `{"name":"<tool>","arguments":{...}}`; the key is `name`, not `tool`. | `src/http_api.py` · `scripts/unitares` · `tests/test_http_endpoints.py` |
| Warmup and baselining | Three stages are distinct with the current constants: check-ins 1–2 use the Φ cold-start prior because behavioral confidence is below 0.3; from check-in 3 the behavioral assessment is authoritative but uses fixed universal thresholds; at check-in 25, `baseline_confidence >= 0.8` against the 30-update target enables self-relative z-score scoring. Absolute safety floors and basin gates remain in force. | `docs/EISV_COMPUTATION.md` · `src/behavioral_state.py` · `src/cold_start_risk_confirmation.py` · `src/governance_monitor.py` |
| Post-warmup verdict authority | Post-warmup the verdict IS the behavioral assessment (z-scores vs the agent's own baseline, absolute floors and basin gates always in force); Φ is telemetry by default. | `docs/EISV_COMPUTATION.md` · `config/governance_config.py` (`phi_telemetry_only`) |
| Outcome-evidence trust | `record_result()` records an outcome claim and its provenance; it does not make an agent-authored result independently true. CI-, tool-, or operator-authored evidence can be a stronger calibration anchor. If the monitored process controls both confidence and every outcome record, it can forge a consistent story. | `docs/SCOPE_AND_THREAT_MODEL.md` · `src/grounding/outcome_anchors.py` · `src/mcp_handlers/observability/outcome_events.py` |
| Current public ablation read | The frozen 2026-08-09 trusted-anchor matrix labels all 12 overall scope/window/lead slices `NOISE-LEVEL` against the best-of-candidates null (selective p = 0.070–0.567). Unadjusted lift is not selection-adjusted evidence, and no prevention is demonstrated. | `docs/operations/eisv-ablation-frozen-2026-08-09.md` · `docs/REVIEWER_GUIDE.md` |
| What that read licenses | A non-detection, not a demonstrated negative and not a ceiling on forecasting power. Predictive lift is **unresolved** pending the pre-registered 2026-12-01 read. The instrument's power on a cohort of that shape is measured, not assumed. | `docs/operations/falsifiability-power-audit-2026-08-23.md` · `scripts/analysis/ablation_power_probe.py` · `docs/proposals/eisv-outcome-grounding-stop-rule-v0.md` |

## Current Architecture Truth

These files are the canonical runtime sources for current behavior:

| Topic | Canonical source | Why it matters |
|------|-------------------|----------------|
| Shared runtime state and monitor access | `src/agent_state.py`, `src/mcp_handlers/shared.py` | Defines the live singleton/facade that many handlers dereference |
| Core governance runtime | `src/governance_monitor.py` | Initializes dual-log grounding, behavioral state, ODE diagnostics, calibration hooks, and verdict flow |
| Dual-log grounding | `src/dual_log/continuity.py` | Cross-checks reflective inputs against operational signals and tool-derived complexity |
| Behavioral EISV state | `src/behavioral_state.py` | Defines warmup, bootstrap confidence, baselining, and self-relative assessment |
| Behavioral sensor inputs | `src/behavioral_sensor.py` | Shows which observable signals feed behavioral EISV |
| Public semantics returned to operators/agents | `src/services/runtime_queries.py` | Declares behavioral EISV primary, ODE diagnostic, and the surfaced state hierarchy |
| Calibration and outcome ingestion | `src/calibration.py`, `src/auto_ground_truth.py`, `src/grounding/outcome_anchors.py` | Defines outcome records, provenance classes, anchor scopes, and confidence correction; independence depends on the producer |

## How To Use This Page

Use this page differently depending on the task:

- If you are summarizing the system, read `README.md`, then `docs/UNIFIED_ARCHITECTURE.md`, then confirm the relevant claims in the runtime files listed above.
- If you are debugging a discrepancy between docs and behavior, skip straight to the runtime files and treat prose as secondary evidence.
- If you are changing architecture docs, update the relevant live doc and then verify that the runtime source still supports the wording.
- If you are changing runtime semantics, update this file only if the authority map or doc classifications have changed.

This page is not intended to duplicate the full architecture narrative. It is the index that tells you where truth lives and which docs are allowed to summarize that truth.

## Active Docs

These docs should stay aligned with the runtime sources above:

| Doc | Status | Intended use |
|-----|--------|--------------|
| `README.md` | live overview | Public-facing summary and top-level framing |
| `docs/UNIFIED_ARCHITECTURE.md` | canonical prose summary | Human-readable architecture explanation |
| `docs/guides/TROUBLESHOOTING.md` | live troubleshooting guide | Failure diagnosis and practical remediation |
| `docs/operations/OPERATOR_RUNBOOK.md` | live operator guide | Startup, health checks, and operator procedures |
| `docs/guides/START_HERE.md` | thin compatibility entrypoint | Minimal workflow and links outward; should stay short |
| `docs/operations/database_architecture.md` | thin infrastructure reference | Storage/backend facts only; should not restate runtime semantics |
| `docs/operations/DEFINITIVE_PORTS.md` | thin operational registry | Port assignments only; should stay small and factual |

## Specialized Active Docs

These are live but intentionally specialized. They should not be treated as general onboarding or architecture truth:

| Doc | Status | Intended use |
|-----|--------|--------------|
| `docs/guides/CIRS_PROTOCOL.md` | specialized protocol reference | CIRS-specific coordination flows |
| `docs/dev/CIRCUIT_BREAKER_DIALECTIC.md` | specialized recovery reference | Circuit-breaker and dialectic recovery flow |
| `docs/dev/KNOWLEDGE_GRAPH_SEMANTICS.md` | specialized developer reference | Shared-memory write/read, link, and audit semantics |
| `docs/dev/SESSION_KEY_DERIVATION.md` | specialized developer reference | Session-key resolution priority and proof-origin trust model |
| `docs/dev/TOOL_REGISTRATION.md` | specialized developer reference | MCP/tool registration work |

## Supporting Non-Canonical Artifacts

These are useful, but they are not runtime authority:

| Artifact | Status | Intended use |
|-----|--------|--------------|
| Paper / preprint snapshots | versioned research framing | Explain a dated architecture and deployment snapshot; not the live canonical system description |
| `docs/CHANGELOG.md` | release history | Track what changed over time; not a substitute for current runtime semantics |

## Known Stale-Risk Patterns

If you see these in active docs, treat them as drift candidates:

- "system operates on agent-reported inputs"
- descriptions implying self-report is the sole or primary substrate
- descriptions implying ODE state directly drives verdicts
- descriptions that omit dual-log grounding from the live architecture

Additional stale-risk patterns:

- long onboarding docs that quietly become second architecture manuals
- operational docs that start restating runtime semantics
- niche deployment guides that read like the default local path
- references to archived design language without an explicit historical label

## Maintenance Rules

Use these rules when deciding whether to edit, shrink, or add a doc:

1. If the content is broad and user-facing, prefer updating an existing live doc rather than creating a new one.
2. If the content is narrow and task-specific, mark it as specialized so agents do not treat it as default guidance.
3. If a doc mainly points to other docs, keep it thin and add an explicit status line saying so.
4. If a statement describes runtime behavior, verify it against the canonical source files before merging.
5. If a doc becomes historical, delete it or note its status inline. Do not preserve stale docs.

The goal is not to minimize documentation at all costs. The goal is to keep the active docs set small enough that agents can form the right model quickly, while still preserving specialized references for the narrower workflows that genuinely need them.
