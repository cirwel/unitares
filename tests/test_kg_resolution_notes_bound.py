"""The details value a knowledge update stores is bounded.

resolution_notes are appended to details as a timestamped block, and nothing
stopped the field growing. On AGE, details is a graph-node property
interpolated into Cypher; GraphMixin._sanitize_cypher_param refuses a string
over 128 KiB, the whole update rolls back, update_discovery returns False, and
the handler reported "Discovery not found" for a finding that exists. The
handler now measures the value it would store (the stored details or the
details the call sends, earlier notes included, plus the new block) against
MAX_UPDATED_DETAILS_LEN and refuses with INVALID_PARAM before storage.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.mixins.graph import GraphMixin
from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge import handlers as kg_handlers
from src.mcp_handlers.knowledge.handlers import (
    _build_discovery_updates,
    _KnowledgeUpdateRequest,
    _UpdateResponseError,
)
from src.mcp_handlers.knowledge.limits import MAX_DETAILS_LEN, MAX_UPDATED_DETAILS_LEN
from tests.helpers import parse_result

# What store appends to details it truncated at MAX_DETAILS_LEN.
_STORE_TRUNCATION_MARKER = "... [truncated]"


@pytest.fixture(autouse=True)
def _fixed_clock():
    """isoformat() drops the fraction when microseconds are zero, which would
    change the block's length between two calls in one test."""
    with patch(
        "src.mcp_handlers.knowledge.handlers._utc_now_iso",
        return_value="2026-09-27T00:00:00.000001+00:00",
    ):
        yield


class _GraphHost(GraphMixin):
    """Bare mixin host: only the sanitizer and interpolation are used."""

    def __init__(self):
        self._age_graph = "test_graph"


def _request(**overrides):
    fields = {
        "arguments": {"discovery_id": "d-1"},
        "discovery_id": "d-1",
        "status": None,
        "details": None,
        "resolution_note": None,
        "summary": None,
        "severity": None,
        "discovery_type": None,
        "tags": None,
        "superseded_by": None,
        "closure_class": None,
        "closure_evidence": None,
    }
    fields.update(overrides)
    return _KnowledgeUpdateRequest(**fields)


def _stored(**overrides):
    fields = dict(id="d-1", agent_id="a-1", type="bug_found", summary="s", status="open")
    fields.update(overrides)
    return DiscoveryNode(**fields)


def _refusal(exc) -> dict:
    return json.loads(exc.value.response.text)


def _block_overhead(base: str) -> int:
    """Characters the appended block adds besides the note itself."""
    updates, _ = _build_discovery_updates(
        _request(resolution_note="x"), _stored(details=base)
    )
    return len(updates["details"]) - len(base) - 1


# ---------------------------------------------------------------------------
# Where the bound sits
# ---------------------------------------------------------------------------


def test_the_bound_leaves_room_for_notes_on_a_finding_stored_at_the_cap():
    assert MAX_DETAILS_LEN + len(_STORE_TRUNCATION_MARKER) < MAX_UPDATED_DETAILS_LEN


def test_the_bound_is_below_the_cypher_parameter_limit():
    assert MAX_UPDATED_DETAILS_LEN < GraphMixin._MAX_PARAM_LENGTH
    # And a value at the bound is one the AGE wrapper accepts.
    assert _GraphHost()._sanitize_cypher_param("D" * MAX_UPDATED_DETAILS_LEN)


# ---------------------------------------------------------------------------
# The value measured is the one storage would write
# ---------------------------------------------------------------------------


class TestNotesAreMeasuredWithWhatTheyAreAppendedTo:
    def test_notes_that_land_exactly_on_the_bound_pass(self):
        base = "b" * 50_000
        note = "n" * (MAX_UPDATED_DETAILS_LEN - len(base) - _block_overhead(base))
        updates, _ = _build_discovery_updates(
            _request(status="resolved", resolution_note=note), _stored(details=base)
        )
        assert len(updates["details"]) == MAX_UPDATED_DETAILS_LEN
        assert updates["status"] == "resolved"

    def test_one_character_over_is_refused_and_says_how_much_fits(self):
        base = "b" * 50_000
        room = MAX_UPDATED_DETAILS_LEN - len(base) - _block_overhead(base)
        with pytest.raises(_UpdateResponseError) as refused:
            _build_discovery_updates(
                _request(status="resolved", resolution_note="n" * (room + 1)),
                _stored(details=base),
            )
        body = _refusal(refused)
        assert body["error_code"] == "INVALID_PARAM"
        assert f"{MAX_UPDATED_DETAILS_LEN + 1:,} characters" in body["error"]
        assert f"the limit is {MAX_UPDATED_DETAILS_LEN:,}" in body["error"]
        assert "Nothing was changed" in body["error"]
        assert "not found" not in body["error"].lower()
        action = body["recovery"]["action"]
        assert f"Shorten resolution_notes to at most {room:,} characters" in action
        assert "response_to" in action

    def test_earlier_notes_count(self):
        """A short note is refused when the notes already on the record fill
        the field: each closing update appends, so growth is cumulative."""
        earlier = "b" * (MAX_UPDATED_DETAILS_LEN - 100)
        with pytest.raises(_UpdateResponseError):
            _build_discovery_updates(
                _request(resolution_note="n" * 200), _stored(details=earlier)
            )

    def test_the_details_this_call_sends_are_the_base_when_both_are_sent(self):
        stored_small = _stored(details="short")
        sent = "s" * (MAX_UPDATED_DETAILS_LEN - 10)
        with pytest.raises(_UpdateResponseError) as refused:
            _build_discovery_updates(
                _request(details=sent, resolution_note="n" * 100), stored_small
            )
        assert "The details this call sends leave no room" in _refusal(refused)[
            "recovery"
        ]["action"]

    def test_a_finding_whose_details_fill_the_field_is_told_to_close_without_notes(self):
        full = "b" * MAX_UPDATED_DETAILS_LEN
        with pytest.raises(_UpdateResponseError) as refused:
            _build_discovery_updates(
                _request(status="resolved", resolution_note="done"),
                _stored(details=full),
            )
        action = _refusal(refused)["recovery"]["action"]
        assert action.startswith("This finding's stored details leave no room")
        assert "without resolution_notes" in action
        assert "'discovery_id': 'd-1'" in action

    def test_a_finding_stored_at_the_details_cap_still_takes_notes(self):
        at_cap = "b" * MAX_DETAILS_LEN + _STORE_TRUNCATION_MARKER
        updates, _ = _build_discovery_updates(
            _request(status="resolved", resolution_note="n" * 16_000),
            _stored(details=at_cap),
        )
        assert updates["details"].startswith(at_cap)

    def test_details_sent_alone_are_held_to_the_same_bound(self):
        """details is the same stored field; replacing it past the bound fails
        on AGE exactly as appending past it does."""
        with pytest.raises(_UpdateResponseError) as refused:
            _build_discovery_updates(
                _request(details="d" * (MAX_UPDATED_DETAILS_LEN + 1)), _stored()
            )
        body = _refusal(refused)
        assert body["error_code"] == "INVALID_PARAM"
        assert body["error"].startswith(
            f"details is {MAX_UPDATED_DETAILS_LEN + 1:,} characters"
        )

    def test_an_update_that_writes_no_details_is_not_measured(self):
        """A status change on a row whose stored details are already over the
        bound (written before it existed) is not blocked by them."""
        updates, _ = _build_discovery_updates(
            _request(status="resolved"),
            _stored(details="b" * (MAX_UPDATED_DETAILS_LEN + 5_000)),
        )
        assert "details" not in updates
        assert updates["status"] == "resolved"


# ---------------------------------------------------------------------------
# Through the handler and the AGE backend's real update path
# ---------------------------------------------------------------------------


def _age_graph(stored: DiscoveryNode):
    """KnowledgeGraphAGE whose graph_query runs the real Cypher interpolation.

    The only fake parts are the connection and the SQL-row sync: the SET
    clause is built by update_discovery and every parameter goes through
    GraphMixin._sanitize_cypher_param, which is where an oversized value
    used to fail.
    """
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE

    host = _GraphHost()
    db = MagicMock()
    db.graph_available = AsyncMock(return_value=True)

    async def graph_query(cypher, params, conn=None):
        host._interpolate_params(cypher, params)
        return [{"d.id": params["discovery_id"]}]

    db.graph_query = AsyncMock(side_effect=graph_query)

    @asynccontextmanager
    async def transaction():
        yield MagicMock()

    db.transaction = transaction

    kg = KnowledgeGraphAGE()
    kg._db = db

    async def _get_db():
        return db

    kg._get_db = _get_db  # type: ignore[assignment]
    kg._sync_updated_discovery_row = AsyncMock()  # type: ignore[assignment]
    kg._refresh_embedding = AsyncMock()  # type: ignore[assignment]
    kg.get_discovery = AsyncMock(return_value=stored)  # type: ignore[assignment]
    return kg, db


@pytest.fixture
def handler_env():
    server = MagicMock()
    server.agent_metadata = {}
    with (
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch("src.mcp_handlers.shared.get_mcp_server", return_value=server),
        patch("src.mcp_handlers.knowledge.handlers.mcp_server", server),
    ):
        yield


async def _update_through_age(stored: DiscoveryNode, arguments: dict):
    kg, db = _age_graph(stored)
    with patch(
        "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
        new_callable=AsyncMock,
        return_value=kg,
    ):
        data = parse_result(
            await kg_handlers.handle_update_discovery_status_graph(arguments)
        )
    return data, db


@pytest.mark.asyncio
async def test_notes_past_the_cypher_limit_are_refused_not_reported_missing(handler_env):
    """The reported defect: stored details plus a closing note over 128 KiB.

    Before the bound, the Cypher sanitizer raised, the update rolled back, and
    the caller was told the discovery did not exist.
    """
    stored = _stored(details="b" * 120_000)
    data, db = await _update_through_age(
        stored,
        {
            "agent_id": "a-1",
            "discovery_id": "d-1",
            "status": "resolved",
            "resolution_notes": "n" * 12_000,
        },
    )
    assert data["success"] is False
    assert data["error_code"] == "INVALID_PARAM"
    assert "not found" not in data["error"].lower()
    assert "resolution_notes" in data["recovery"]["action"]
    db.graph_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_notes_within_the_bound_reach_the_graph_and_close_it(handler_env):
    stored = _stored(details="b" * 60_000)
    data, db = await _update_through_age(
        stored,
        {
            "agent_id": "a-1",
            "discovery_id": "d-1",
            "status": "resolved",
            "resolution_notes": "fixed in the running build",
        },
    )
    assert data["success"] is True
    db.graph_query.assert_awaited_once()
    params = db.graph_query.await_args.args[1]
    assert params["val_status"] == "resolved"
    assert params["val_details"].startswith("b" * 60_000)
    assert params["val_details"].endswith("fixed in the running build")
