"""Knowledge-surface fixes from the 2026-10-16 AX dogfood read.

docs/evaluations/ax-dogfood/read-2026-10-16.md, accidental-burden items 2, 3,
6 and 9. Item 1 (more_results without an explicit limit) is pinned in
tests/test_agent_experience_envelope.py next to the rows' other tests.
"""

import re

import pytest

from src.knowledge_graph import VALID_RESPONSE_TYPES
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.schemas.knowledge import KnowledgeParams
from src.tool_modes import progressive_aware_hint

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")


def _search_payload(bases):
    return {
        "success": True,
        "results": [
            {
                "id": f"d{i}",
                "summary": f"finding {i}",
                "type": "note",
                "status": "open",
                "relevance": 0.5 if basis == "semantic_similarity" else 0.016,
                "relevance_basis": basis,
            }
            for i, basis in enumerate(bases)
        ],
        "total_count": len(bases),
    }


# Item 2: two relevance scales in one list.

def test_mixed_relevance_bases_get_one_note():
    env = build_experience_envelope(
        "search_shared_memory",
        "knowledge",
        _search_payload(["semantic_similarity", "lexical_rank_only", "semantic_similarity"]),
    )
    assert "not comparable" in env["state_summary"]["relevance_note"]


def test_single_relevance_basis_has_no_note():
    env = build_experience_envelope(
        "search_shared_memory",
        "knowledge",
        _search_payload(["semantic_similarity"] * 3),
    )
    assert "relevance_note" not in env["state_summary"]


# Item 3: hints must name a route the progressive surface can take.

@pytest.mark.parametrize(
    "hint, expected",
    [
        (
            "Open one with knowledge(action='details', discovery_id='...').",
            "Open one with use_tool(tool_name='knowledge', arguments="
            '{"action": "details", "discovery_id": "..."}).',
        ),
        (
            "knowledge(action='details', discovery_id=..., offset=latest_offset)",
            "use_tool(tool_name='knowledge', arguments="
            '{"action": "details", "discovery_id": "...", "offset": latest_offset})',
        ),
        (
            "knowledge(action='details')",
            "use_tool(tool_name='knowledge', arguments={\"action\": \"details\"})",
        ),
    ],
)
def test_knowledge_hints_name_use_tool(hint, expected):
    assert progressive_aware_hint(hint) == expected


def test_prose_that_is_not_a_call_is_left_alone():
    assert progressive_aware_hint("the knowledge(graph) grows") == "the knowledge(graph) grows"


def test_lifted_resolution_notes_hint_is_routed():
    payload = _search_payload(["semantic_similarity"] * 3)
    # The hint is kept only while a shown row carries notes.
    payload["results"][0]["resolution_notes"] = {
        "source": "details_text", "count": 1, "first_offset": 5, "latest_offset": 5,
    }
    payload["resolution_notes_hint"] = (
        "Read with knowledge(action='details', discovery_id=..., offset=latest_offset)."
    )
    env = build_experience_envelope("search_shared_memory", "knowledge", payload)
    hint = env["resolution_notes_hint"]
    assert "knowledge(action=" not in hint
    assert "use_tool(tool_name='knowledge'" in hint


# Item 6: response_to lists its response_type values.

def test_response_to_description_lists_every_response_type():
    description = KnowledgeParams.model_fields["response_to"].description
    listed = set(re.findall(r"[a-z_]+", description.split("response_type is one of", 1)[1]))
    assert listed == set(VALID_RESPONSE_TYPES)


# Item 9: no router vocabulary on the store alias.

def test_store_finding_fields_carry_no_router_vocabulary():
    from scripts.diagnostics import tool_surface_cost as cost

    for definition in cost._definitions("progressive", "catalog"):
        data = definition if isinstance(definition, dict) else definition.model_dump(
            by_alias=True, exclude_none=True
        )
        if data.get("name") != "store_finding":
            continue
        for name, prop in data["inputSchema"]["properties"].items():
            text = prop.get("description", "")
            assert "action=" not in text, (name, text)
            assert "S22" not in text, (name, text)
        return
    pytest.fail("store_finding is not on the progressive surface")
