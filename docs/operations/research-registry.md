# Research Registry

The research registry records agent-network experiments in one queryable place.
It is file-backed by design: no migration is required, and records can link out
to KG discoveries, outcome events, findings, traces, or external artifacts.

Default storage:

```text
~/.local/state/unitares/research-runs/*.json
```

Override with:

```bash
UNITARES_RESEARCH_REGISTRY_DIR=/path/to/research-runs
```

## MCP

Read actions are available before onboarding:

```text
research_registry(action="query", research_area="science-of-agent-networks")
research_registry(action="stats")
research_registry(action="get", run_id="...")
```

Write action is identity-gated:

```text
research_registry(action="record", run={
  "run_id": "agent-network-smoke-001",
  "title": "Agent-network smoke run",
  "status": "completed",
  "scenario": {"id": "mixed-motive-routing", "name": "Mixed motive routing"},
  "topology": {"kind": "small_world", "nodes": 4, "edges": 5},
  "population": [{"agent_class": "planner", "count": 2}],
  "metrics": [{"name": "task_success_rate", "source": "harness"}],
  "exogenous_anchor": {"source": "harness", "outcome": "task_success_rate"},
  "artifacts": [{"kind": "trace", "uri": "kg:disc-123"}],
  "research_areas": ["science-of-agent-networks"],
  "tags": ["grant", "network"]
})
```

## REST

```text
GET /v1/research/runs
GET /v1/research/runs?research_area=science-of-agent-networks&grounding=anchored
GET /v1/research/runs/{run_id}
GET /v1/research/stats
```

The dashboard exposes the same data under the `Research` tab.

## Rigor Checklist

Each record returns a derived `rigor_checklist`:

- `scenario`
- `topology`
- `population`
- `metrics`
- `exogenous_grounding`
- `artifacts`

`grounding_status` is `anchored` when `exogenous_anchor` cites an external
source/dataset/outcome, `linked` when only linked KG/outcome ids exist, and
`missing` when neither is present.
