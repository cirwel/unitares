from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.research_registry import (
    ResearchRegistryError,
    grounding_status,
    query_research_runs,
    record_research_run,
    research_registry_stats,
    rigor_checklist,
)


def _sample_run(**overrides):
    run = {
        "run_id": "coop-agent-networks-smoke",
        "title": "Cooperative agent-network smoke run",
        "status": "completed",
        "scenario": {"id": "mixed-motive-routing", "name": "Mixed motive routing"},
        "topology": {"kind": "small_world", "nodes": 4, "edges": 5},
        "population": [
            {"agent_class": "planner", "count": 2},
            {"agent_class": "skeptic", "count": 1},
        ],
        "metrics": [{"name": "task_success_rate", "source": "harness"}],
        "artifacts": [{"kind": "trace", "uri": "kg:disc-1"}],
        "exogenous_anchor": {"source": "harness", "outcome": "task_success_rate"},
        "research_areas": ["science-of-agent-networks"],
        "tags": ["grant", "network"],
    }
    run.update(overrides)
    return run


def _parse_tool_result(result):
    return json.loads(result[0].text)


def test_record_and_query_research_runs(tmp_path):
    record = record_research_run(_sample_run(), root=tmp_path)

    assert record["run_id"] == "coop-agent-networks-smoke"
    assert grounding_status(record) == "anchored"
    assert all(rigor_checklist(record).values())

    result = query_research_runs(
        research_area="science-of-agent-networks",
        tag="grant",
        root=tmp_path,
    )
    assert result["count"] == 1
    assert result["runs"][0]["run_id"] == "coop-agent-networks-smoke"
    assert result["runs"][0]["rigor_complete"] is True


def test_record_requires_core_research_shape(tmp_path):
    with pytest.raises(ResearchRegistryError, match="population"):
        record_research_run(
            {
                "scenario": {"id": "missing-population"},
                "topology": {"kind": "line"},
            },
            root=tmp_path,
        )


def test_stats_group_grounding_and_research_area(tmp_path):
    record_research_run(_sample_run(run_id="anchored"), root=tmp_path)
    record_research_run(
        _sample_run(
            run_id="linked",
            exogenous_anchor={},
            linked_knowledge_ids=["disc-1"],
            artifacts=[],
        ),
        root=tmp_path,
    )

    stats = research_registry_stats(root=tmp_path)
    assert stats["total"] == 2
    assert stats["by_grounding"] == {"anchored": 1, "linked": 1}
    assert stats["by_research_area"]["science-of-agent-networks"] == 2
    assert stats["rigor_complete"] == 1
    assert stats["rigor_incomplete"] == 1


@pytest.mark.asyncio
async def test_research_registry_mcp_record_and_query(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_RESEARCH_REGISTRY_DIR", str(tmp_path))
    from src.mcp_handlers.research_registry import handle_research_registry

    recorded = _parse_tool_result(
        await handle_research_registry({"action": "record", "run": _sample_run()})
    )
    assert recorded["success"] is True
    assert recorded["grounding_status"] == "anchored"

    queried = _parse_tool_result(
        await handle_research_registry({
            "action": "query",
            "research_area": "science-of-agent-networks",
        })
    )
    assert queried["count"] == 1
    assert queried["runs"][0]["run_id"] == "coop-agent-networks-smoke"


@pytest.mark.asyncio
async def test_research_registry_http_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_RESEARCH_REGISTRY_DIR", str(tmp_path))
    record_research_run(_sample_run(), root=tmp_path)

    from src import http_api

    monkeypatch.setattr(
        http_api,
        "_check_http_auth",
        lambda request, *, http_api_token: True,
    )

    list_req = SimpleNamespace(
        headers={},
        query_params={"tag": "grant", "limit": "10"},
        path_params={},
    )
    list_resp = await http_api.http_research_runs(list_req)
    list_body = json.loads(list_resp.body)
    assert list_body["success"] is True
    assert list_body["count"] == 1

    get_req = SimpleNamespace(
        headers={},
        query_params={},
        path_params={"run_id": "coop-agent-networks-smoke"},
    )
    get_resp = await http_api.http_research_run(get_req)
    get_body = json.loads(get_resp.body)
    assert get_body["success"] is True
    assert get_body["run"]["scenario"]["id"] == "mixed-motive-routing"

    stats_resp = await http_api.http_research_stats(list_req)
    stats_body = json.loads(stats_resp.body)
    assert stats_body["stats"]["total"] == 1


def test_research_registry_schema_is_advertised():
    from src.tool_schemas import get_pydantic_schemas

    schema_model = get_pydantic_schemas()["research_registry"]
    props = schema_model.model_json_schema()["properties"]
    assert "research_area" in props
    assert "exogenous_anchor" in props
