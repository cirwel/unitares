"""An agent can reach appended evidence without mistaking a headline for it."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge import handlers
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from tests.helpers import parse_result


def note(text, stamp="2026-10-13T12:00:00.123456+00:00"):
    return f"Resolution notes ({stamp}):\n{text}"


def document(details, **kwargs):
    return DiscoveryNode(
        id=kwargs.pop("id", "finding"), agent_id="writer", type="insight",
        summary="Hold the public edit pending validation.", details=details,
        timestamp="2026-06-19T00:00:00+00:00", **kwargs,
    )


def search_payload(monkeypatch, documents):
    monkeypatch.setattr(handlers, "_resolve_agent_display", lambda _: {"display_name": "writer"})
    state = handlers._KnowledgeSearchState(
        request=handlers._parse_knowledge_search_request({"query": "public edit"}),
        graph=None,
    )
    state.results = documents
    rows = handlers._serialize_search_discoveries(state, include_details=False)
    return {"success": True, **handlers._base_search_response(
        state, rows, auto_details=False, include_details=False,
    )}


@pytest.mark.parametrize("mode", ["lean", "compact", "full"])
def test_search_modes_keep_offsets_without_reinterpreting_a_resolved_claim(monkeypatch, mode):
    details = "Original evidence.\n\n" + note("Validation passed; the hold is lifted.")
    doc = document(details, status="resolved", closure_class="fix_verified")
    payload = search_payload(monkeypatch, [doc])
    if mode == "lean":
        payload = handlers._lean_search_payload(payload)
    result = build_experience_envelope("search_shared_memory", "knowledge", payload, {"response_mode": mode})
    row = result["raw_governance"]["discoveries"][0] if mode == "full" else result["memory_suggestions"][0]
    assert row["resolution_notes"] == {
        "source": "details_text", "count": 1,
        "first_offset": details.index("Resolution notes"),
        "latest_offset": details.index("Resolution notes"),
    }
    assert row["summary"] == doc.summary
    assert row["status"] == "resolved"
    assert row["closure_class"] == "fix_verified"
    assert "latest_offset" in result["resolution_notes_hint"]


@pytest.mark.asyncio
async def test_unicode_offsets_reach_full_notes_and_preserve_earlier_corrections(monkeypatch):
    original = "原🧭e\u0301\n" * 1300
    first = note("Staging approved. Production remains held.")
    second = note("Production approved after separate validation.", "2026-10-14T01:00:00Z")
    last = note("Linked the merged PR.", "2026-10-14T02:00:00+00:00")
    details = "\n\n".join([original, first, second, last])
    doc = document(details, status="open")
    row = search_payload(monkeypatch, [doc])["discoveries"][0]
    pointer = row["resolution_notes"]
    assert pointer["count"] == 3
    assert pointer["latest_offset"] == details.index(last)
    assert pointer["latest_offset"] > 6000
    monkeypatch.setattr(handlers, "get_knowledge_graph", AsyncMock(return_value=SimpleNamespace(
        get_discovery=AsyncMock(return_value=doc))))
    monkeypatch.setattr(handlers, "_broadcast_knowledge_read", AsyncMock())
    # The ordinary first page cannot show the correction. The emitted offsets
    # use exactly the same Unicode character units as the existing details API.
    first_page = parse_result(await handlers.handle_get_discovery_details({"discovery_id": doc.id}))
    assert "Production approved" not in first_page["details"]
    for key, expected in (("latest_offset", last), ("first_offset", "\n\n".join([first, second, last]))):
        page = parse_result(await handlers.handle_get_discovery_details({
            "discovery_id": doc.id, "offset": pointer[key],
        }))
        assert page["details"] == expected
        assert page["discovery"]["resolution_notes"] == pointer
        assert page["pagination"]["offset"] == pointer[key]
    assert doc.details == details


@pytest.mark.parametrize("details", [None, "A resolved incident confirms its original cause.",
    "Inline Resolution notes (2026-10-13T12:00:00Z):\nnot a block",
    note("not a dated block", "not-a-date"), note("invalid calendar date", "2026-99-99T12:00:00Z"),
])
def test_no_pointer_is_invented_without_a_recognizable_note_block(monkeypatch, details):
    row = search_payload(monkeypatch, [document(details, status="resolved")])["discoveries"][0]
    assert "resolution_notes" not in row


def test_notes_are_text_locations_not_authenticated_updates(monkeypatch):
    # A writer can put a header in details. It is a navigational aid, never a
    # signed receipt, author identity, current-policy verdict or ranking input.
    row = search_payload(monkeypatch, [document(note("A claim, not verified."))])["discoveries"][0]
    assert row["resolution_notes"]["source"] == "details_text"
    assert row["authority"]["tier"] == "native_finding"
    assert row["status"] == "open"


def test_more_result_rows_keep_note_locations_inside_existing_budget(monkeypatch):
    docs = [document("Body.\n\n" + note("Updated evidence."), id=f"d{i}") for i in range(10)]
    payload = search_payload(monkeypatch, docs)
    env = build_experience_envelope("search_shared_memory", "knowledge", payload, {"limit": 10})
    assert len(env["memory_suggestions"]) == 3
    assert len(env["more_results"]) == 7
    assert all(row["resolution_notes"]["latest_offset"] == 7 for row in env["more_results"])
    extra = env.pop("more_results")
    assert len(json.dumps(env, ensure_ascii=False).encode()) <= 3000
    assert len(json.dumps(extra, ensure_ascii=False).encode()) <= 2000


@pytest.mark.asyncio
async def test_agent_get_and_imported_inline_cap_keep_full_text_offsets(monkeypatch):
    details = "Old context. " * 700 + "\n\n" + note("Later correction.")
    doc = document(details, tags=["source-bridge-memory"])
    monkeypatch.setattr(handlers, "get_knowledge_graph", AsyncMock(return_value=SimpleNamespace(
        get_agent_discoveries=AsyncMock(return_value=[doc]))))
    monkeypatch.setattr(handlers, "require_registered_agent", lambda _: ("writer", None))
    monkeypatch.setattr(handlers, "_agent_display_for_response", lambda *_: {"display_name": "writer"})
    monkeypatch.setattr(handlers, "_broadcast_knowledge_read", AsyncMock())
    payload = parse_result(await handlers.handle_get_knowledge_graph({"agent_id": "writer", "include_details": True}))
    row = payload["discoveries"][0]
    assert row["resolution_notes"]["latest_offset"] == details.index("Resolution notes")
    assert row["details_truncated"] is True
    assert row["imported_memory"]["trust"] == "unreviewed"
