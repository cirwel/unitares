"""Advertised parameter texts corrected in interface contract 1.20.0.

Each lagged its handler: knowledge's discovery_type said "Required for
action=store" though the store handler defaults it to note, and named none of
the other actions that read it; observe's target_agent_id, until and agent_ids
named fewer actions than read them; and dialectic's issue_description named
request but not quick, which refuses to run without it. The review of the
change added four more: target_agent_id must not promise a UUID where audit
rows are stored by name, update_finding's discovery_type must not say an
omitted type defaults to note, closure_class must say a class may be set alone
on a finding that is already closed, and closure_evidence must state its
bound. These tests derive the facts from the code, so the texts cannot drift
from it again silently.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.schemas.dialectic import DialecticParams
from src.mcp_handlers.schemas.knowledge import KnowledgeParams
from src.mcp_handlers.schemas.observability import ObserveParams


def _named_actions(model, text: str) -> set[str]:
    actions = set(model.ACTION_FIELDS)
    return {
        action for action in actions if re.search(rf"(?<![\w]){action}(?![\w])", text)
    }


def _actions_reading(model, field: str) -> set[str]:
    return {action for action, fields in model.ACTION_FIELDS.items() if field in fields}


@pytest.mark.parametrize(
    "model,field",
    [
        (ObserveParams, "target_agent_id"),
        (ObserveParams, "until"),
        (ObserveParams, "agent_ids"),
        (DialecticParams, "issue_description"),
    ],
)
def test_the_description_names_every_action_that_reads_the_field(model, field):
    """ACTION_FIELDS is held to the handlers by test_describe_lite_action_parameters;
    the description is held to ACTION_FIELDS here."""
    description = model.model_fields[field].description
    assert _named_actions(model, description) == _actions_reading(model, field), (
        f"{model.__name__}.{field}: {description!r}"
    )


def test_store_defaults_discovery_type_and_the_description_says_so():
    from src.mcp_handlers.knowledge.handlers import _parse_single_store_request

    description = KnowledgeParams.model_fields["discovery_type"].description
    brief = KnowledgeParams.model_fields["discovery_type"].json_schema_extra["brief"]
    assert "Required" not in description
    assert "store defaults it to note" in description and "defaults to note" in brief

    request = _parse_single_store_request(
        {"summary": "a finding"}, "a-1", None, False
    )
    assert request.discovery_type == "note"


@pytest.mark.asyncio
async def test_audit_events_passes_target_agent_id_through_unresolved():
    """The description says audit_events does not resolve a label; it doesn't."""
    from src.mcp_handlers.observability.handlers import handle_audit_events

    query = AsyncMock(return_value=[])
    aggregate = AsyncMock(return_value=[])
    with (
        patch("src.audit_db.query_audit_events_async", query),
        patch("src.audit_db.aggregate_audit_events_async", aggregate),
    ):
        await handle_audit_events(
            {"event_type": "governance_decision", "target_agent_id": "some-label"}
        )
    assert query.await_args.kwargs["agent_id"] == "some-label"
    assert aggregate.await_args.kwargs["agent_id"] == "some-label"
    assert "do not resolve labels" in ObserveParams.model_fields["target_agent_id"].description


def test_target_agent_id_does_not_promise_a_uuid_for_audit_events():
    """audit.events stores some writers by name, not by UUID.

    The stuck-agent sweep writes its audit entry as agent_id="system"; residents
    such as sentinel record their name too. A text telling the caller to pass a
    UUID steers it away from exactly those rows.
    """
    from pathlib import Path

    stuck = (
        Path(__file__).resolve().parent.parent / "src/mcp_handlers/lifecycle/stuck.py"
    ).read_text()
    assert "audit_logger._write_entry(AuditEntry(" in stuck
    assert 'agent_id="system"' in stuck

    field = ObserveParams.model_fields["target_agent_id"]
    assert "stored by name" in field.description and "system" in field.description
    assert "a UUID, and" not in field.description
    brief = field.json_schema_extra["brief"]
    assert "exact stored agent_id" in brief
    assert "UUID, no label" not in brief


def test_the_router_discovery_type_names_every_action_that_reads_it():
    """"note" is an action and a type, so the exact-set check above cannot apply;
    every action that reads the field must still be named, with its default."""
    import inspect

    from src.mcp_handlers.knowledge.handlers import handle_promote_memory_claim

    description = KnowledgeParams.model_fields["discovery_type"].description
    reading = _actions_reading(KnowledgeParams, "discovery_type")
    assert reading == {"store", "search", "update", "promote"}
    assert reading <= _named_actions(KnowledgeParams, description), description
    assert 'arguments.get("discovery_type") or "insight"' in inspect.getsource(
        handle_promote_memory_claim
    )
    assert "promote to insight" in description
    assert "omitted keeps the stored one" in description


def test_update_finding_says_an_omitted_type_is_kept_not_defaulted():
    """update_finding pins action=update, where no type is written unless passed."""
    from src import mcp_server
    from src.alias_schema import ALIAS_SCHEMA_PROPERTY_OVERRIDES
    from src.knowledge_graph import DiscoveryNode
    from src.mcp_handlers.knowledge.handlers import (
        _build_discovery_updates,
        _parse_knowledge_update_request,
    )

    request = _parse_knowledge_update_request({"discovery_id": "d-1", "status": "resolved"})
    updates, _ = _build_discovery_updates(
        request,
        DiscoveryNode(id="d-1", agent_id="a-1", type="bug_found", summary="s", status="open"),
    )
    assert "type" not in updates

    override = ALIAS_SCHEMA_PROPERTY_OVERRIDES["update_finding"]["discovery_type"]
    advertised = mcp_server.mcp._tool_manager.get_tool("update_finding").parameters[
        "properties"
    ]["discovery_type"]["description"]
    for text in (override["description"], override["brief"], advertised):
        assert "defaults to note" not in text, text
        assert "omitted keeps the stored" in text, text


def test_closure_class_description_says_where_a_class_is_admitted():
    """A class alone classifies a finding that is already closed; the text said
    it went only with a closing status."""
    from src.knowledge_graph import (
        CLOSURE_CLASS_ADMITTING_STATUSES,
        CLOSURE_CLASS_CLEARING_STATUSES,
    )

    description = KnowledgeParams.model_fields["closure_class"].description
    for status in CLOSURE_CLASS_ADMITTING_STATUSES | CLOSURE_CLASS_CLEARING_STATUSES:
        assert re.search(rf"(?<![\w]){status}(?![\w])", description), status
    assert "alone on a finding that already has" in description
    assert "reopening clears it" in description


def test_closure_evidence_description_states_the_enforced_bound():
    from src.mcp_handlers.knowledge.limits import MAX_CLOSURE_EVIDENCE_BYTES

    description = KnowledgeParams.model_fields["closure_evidence"].description
    assert MAX_CLOSURE_EVIDENCE_BYTES % 1024 == 0
    assert f"At most {MAX_CLOSURE_EVIDENCE_BYTES // 1024} KiB" in description
