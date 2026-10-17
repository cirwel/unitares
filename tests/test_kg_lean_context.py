"""Lean reads spend context on evidence and preserve routes out of the digest."""
import json
from copy import deepcopy

import pytest

from src.mcp_handlers.middleware.envelope_step import build_experience_envelope


def payload():
    return {"success": True, "count": 1, "discoveries": [{
        "id": "d1", "summary": "The original policy has a later correction.",
        "status": "superseded", "superseded_by": ["d2"], "by": "Writer", "_agent_id": "writer-id",
        "closure_class": "obsolete", "has_details": True,
    }], "discovery_retrieval_options": {
        "current_tier": "digest", "digest": "include_details=false",
        "open_one": "knowledge(action='details', discovery_id='...')",
        "all_inline": "include_details=true (can be large)",
    }}


@pytest.mark.parametrize("mode", [None, "lean"])
def test_lean_has_one_open_route_and_a_small_read_only_next_step(mode):
    source = payload()
    original = deepcopy(source)
    env = build_experience_envelope("search_shared_memory", "knowledge", source,
                                    {} if mode is None else {"response_mode": mode})
    assert source == original
    assert len(json.dumps(env, ensure_ascii=False).encode()) < 1000
    assert "response_options" not in env
    assert env["discovery_retrieval_options"] == {
        "current_tier": "digest",
        "open_one": (
            "use_tool(tool_name='knowledge', arguments="
            '{"action": "details", "discovery_id": "..."})'
        ),
    }
    assert "response_mode='full'" in env["raw_governance_hint"]
    assert "store" not in env["next_action"] and "update" not in env["next_action"]
    assert len(env["next_action"]) < 90
    row = env["memory_suggestions"][0]
    assert row["superseded_by"] == "d2"
    assert row["closure_class"] == "obsolete"
    assert row["by"] == "Writer" and row["agent_id"] == "writer-id"


def test_lean_keeps_requested_details_omission_disclosure():
    env = build_experience_envelope("search_shared_memory", "knowledge", payload(), {
        "response_mode": "lean", "include_details": False,
        "_friendly_search_detail_policy": "digest_before_serialization",
        "_friendly_search_details_requested": True,
    })
    options = env["discovery_retrieval_options"]
    assert options["requested_tier"] == "full_inline"
    assert options["details_omitted_by"] == "response_mode='lean' before serialization"
    assert "details_serialized" not in options


def test_lean_retains_confidence_degradation_and_imported_memory_caveats():
    source = payload()
    source.update(low_confidence=True, confidence_note="Semantic-only; verify evidence.",
                  search_degraded=True, search_degraded_message="Semantic backend unavailable.")
    source["discoveries"][0]["imported_memory"] = {"trust": "unreviewed"}
    env = build_experience_envelope("search_shared_memory", "knowledge", source)
    for key in ("low_confidence", "confidence_note", "search_degraded", "search_degraded_message"):
        assert env[key] == source[key]
    assert env["memory_suggestions"][0]["imported_memory"]["trust"] == "unreviewed"
    assert len(env["next_action"]) < 90


@pytest.mark.parametrize("mode", ["compact", "full"])
def test_diagnostic_modes_keep_mode_and_serialization_guidance(mode):
    env = build_experience_envelope("search_shared_memory", "knowledge", payload(), {"response_mode": mode})
    assert env["response_options"]["current"] == mode
    assert "all_inline" in env["discovery_retrieval_options"]
    assert ("raw_governance" in env) == (mode == "full")
