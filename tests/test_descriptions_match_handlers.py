"""Advertised parameter texts corrected in interface contract 1.20.0.

Each lagged its handler: knowledge's discovery_type said "Required for
action=store" though the store handler defaults it to note; observe's
target_agent_id, until and agent_ids named fewer actions than read them; and
dialectic's issue_description named request but not quick, which refuses to
run without it. These tests derive the facts from the code, so the texts
cannot drift from it again silently.
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
    assert "defaults to note" in description and "defaults to note" in brief

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
