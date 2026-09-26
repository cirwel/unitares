"""Hints on the update_finding and store_finding paths name something that works.

Observed live on 2026-09-26 while closing a finding: the closing note said
``Pass closure_class=['duplicate', 'fix_verified', ...]``. The parameter is a
string, so the literal suggestion failed validation; the note did not say that
``fix_verified`` also needs ``closure_evidence``, so the second attempt failed
too; and the third attempt, which passed validation and was acknowledged with
the class echoed back, left the row's ``closure_class`` NULL. Neither storage
backend writes the column (0 of 1,936 live rows carry a class), so the note
recommended a parameter with no effect.

These tests hold every closure hint to what the code does instead of to a
restated copy of it:

- the evidence each class needs is derived from ``_validate_closure_class``'s
  behaviour, not from its table;
- whether the class is stored is derived from the three storage update paths,
  fed the update payload the real handler builds;
- the call a hint names is checked against the /mcp/ argument model of the tool
  it names, since FastMCP drops undeclared arguments before dispatch.
"""

from __future__ import annotations

import ast
import json
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge import handlers as kg_handlers
from src.mcp_handlers.knowledge.handlers import (
    CLOSURE_CLASSES,
    _build_discovery_updates,
    _KnowledgeUpdateRequest,
    _UpdateResponseError,
    _validate_closure_class,
)
from tests.helpers import parse_result


def _request(**overrides) -> _KnowledgeUpdateRequest:
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


def _derived_evidence_requirements() -> dict[str, tuple[str, ...]]:
    """closure_evidence keys each class needs, read off the validator's refusals.

    An empty evidence object is sent for every class. A class that needs none
    passes; one that does is refused with the keys it is missing, which is all
    of them.
    """
    required: dict[str, tuple[str, ...]] = {}
    for closure_class in sorted(CLOSURE_CLASSES):
        try:
            _validate_closure_class(
                _request(closure_class=closure_class, closure_evidence={}),
                "resolved",
            )
        except _UpdateResponseError as exc:
            message = json.loads(exc.response.text)["error"]
            listed = re.search(r"closure_evidence: (\[[^\]]*\])", message)
            assert listed, f"refusal for {closure_class!r} names no keys: {message}"
            required[closure_class] = tuple(ast.literal_eval(listed.group(1)))
        else:
            required[closure_class] = ()
    return required


def _note(discovery_id: str = "d-1", status: str = "resolved") -> str:
    return kg_handlers._unclassified_closure_note(discovery_id, status)


def test_the_derivation_sees_both_evidence_rules():
    """Guard the fixture: an empty derivation would pass every check below."""
    derived = _derived_evidence_requirements()
    assert derived["fix_verified"] == ("deployed", "observed")
    assert derived["unobserved"] == ("window", "instrument_check")
    assert derived["duplicate"] == ()


# ---------------------------------------------------------------------------
# Whether the class is stored, from the storage layer itself
# ---------------------------------------------------------------------------


class _Capture:
    def __init__(self):
        self.statements: list[str] = []


class _Conn:
    def __init__(self, capture: _Capture):
        self._capture = capture

    async def execute(self, sql, *params):
        self._capture.statements.append(sql)
        return "UPDATE 1"

    async def fetchval(self, sql, *params):
        self._capture.statements.append(sql)
        return "d-1"


class _Pool(_Conn):
    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self._capture)


class _Db:
    """What each backend's update path touches, and nothing else."""

    def __init__(self, capture: _Capture):
        self._capture = capture
        self._pool = _Pool(capture)

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self._capture)

    @asynccontextmanager
    async def transaction(self):
        yield _Conn(self._capture)

    async def graph_available(self):
        return True

    async def graph_query(self, cypher, params, conn=None):
        self._capture.statements.append(cypher)
        return [{"id": "d-1"}]


def _handler_updates() -> dict:
    """The payload the real handler hands the backend for a classified close."""
    updates, _status = _build_discovery_updates(
        _request(
            status="resolved",
            resolution_note="deployed abc123; the new path answered on the live server",
            closure_class="fix_verified",
            closure_evidence={
                "deployed": "abc123 is an ancestor of the running build_sha",
                "observed": "the new response field appeared on a live read",
            },
        ),
        DiscoveryNode(
            id="d-1", agent_id="a-1", type="bug_found", summary="s", details="body"
        ),
    )
    assert updates["closure_class"] == "fix_verified", "premise: the handler sends it"
    return updates


async def _statements_from_every_update_path() -> dict[str, list[str]]:
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE
    from src.storage.knowledge_graph_postgres import KnowledgeGraphPostgres

    out: dict[str, list[str]] = {}

    capture = _Capture()
    age = KnowledgeGraphAGE()
    age._db = _Db(capture)
    age._refresh_embedding = AsyncMock()
    assert await age.update_discovery("d-1", _handler_updates())
    out["age"] = capture.statements

    capture = _Capture()
    age_sql_only = KnowledgeGraphAGE()
    age_sql_only._db = _Db(capture)
    assert await age_sql_only._sql_update_discovery("d-1", _handler_updates())
    out["age_sql_fallback"] = capture.statements

    capture = _Capture()
    postgres = KnowledgeGraphPostgres()
    postgres._db = _Db(capture)
    assert await postgres.update_discovery("d-1", _handler_updates())
    out["postgres"] = capture.statements
    return out


def _writes(statements: list[str], column: str) -> bool:
    return any(re.search(rf"\b{column}\s*=", statement) for statement in statements)


@pytest.mark.asyncio
async def test_persisted_flag_matches_every_storage_update_path():
    """The hints branch on CLOSURE_CLASS_PERSISTED; it must say what storage does."""
    statements = await _statements_from_every_update_path()
    # Premise: the fakes captured real UPDATEs, and the status column is in them.
    for path, captured in statements.items():
        assert _writes(captured, "status"), f"{path}: no status write captured"

    stored = {
        path: _writes(captured, "closure_class")
        for path, captured in statements.items()
    }
    assert len(set(stored.values())) == 1, (
        f"storage paths disagree about closure_class: {stored}. The hints cannot "
        "be true on every backend until they agree."
    )
    assert kg_handlers.CLOSURE_CLASS_PERSISTED is next(iter(stored.values())), (
        f"storage writes closure_class: {stored}, but CLOSURE_CLASS_PERSISTED is "
        f"{kg_handlers.CLOSURE_CLASS_PERSISTED}. Flip it so the hints match."
    )


@pytest.mark.asyncio
async def test_resolution_notes_is_stored_by_every_update_path():
    """The interim note sends the standard to resolution_notes, so it must land."""
    statements = await _statements_from_every_update_path()
    for path, captured in statements.items():
        assert _writes(captured, "details"), f"{path} does not write details"


# ---------------------------------------------------------------------------
# The note on a closing update that declares no class
# ---------------------------------------------------------------------------


def test_note_names_the_classes_as_prose_not_a_list_value():
    for persisted in (False, True):
        with patch.object(kg_handlers, "CLOSURE_CLASS_PERSISTED", persisted):
            note = _note()
        assert "closure_class=[" not in note
        assert "[" not in note, note
        for closure_class in CLOSURE_CLASSES:
            assert f"'{closure_class}'" in note


def test_interim_note_names_each_class_evidence_the_validator_demands():
    with patch.object(kg_handlers, "CLOSURE_CLASS_PERSISTED", False):
        note = _note()
    for closure_class, keys in _derived_evidence_requirements().items():
        if not keys:
            continue
        assert f"'{closure_class}' needs {' and '.join(keys)}" in note, note


def test_interim_note_is_honest_that_the_class_is_not_stored():
    with patch.object(kg_handlers, "CLOSURE_CLASS_PERSISTED", False):
        note = _note()
    assert "resolution_notes, which is stored" in note
    assert "without storing them" in note


def _named_resolution_call(note: str, discovery_id: str) -> str:
    call = re.search(
        rf"(\w+)\(discovery_id='{re.escape(discovery_id)}', resolution_notes='\.\.\.'\)",
        note,
    )
    assert call, f"no resolution_notes call named: {note}"
    return call.group(1)


@pytest.mark.parametrize(
    "note",
    [
        pytest.param(lambda: _note("d-7", "resolved"), id="unclassified-close"),
        pytest.param(
            lambda: kg_handlers._unstored_closure_class_note("d-7", "duplicate"),
            id="unstored-class",
        ),
    ],
)
def test_interim_notes_name_a_resolution_notes_call_that_works_as_written(note):
    """The named call survives the /mcp/ argument model and the update parser."""
    from src import mcp_server

    with patch.object(kg_handlers, "CLOSURE_CLASS_PERSISTED", False):
        text = note()
    tool_name = _named_resolution_call(text, "d-7")
    tool = mcp_server.mcp._tool_manager.get_tool(tool_name)
    received = tool.fn_metadata.arg_model.model_validate(
        {
            "discovery_id": "d-7",
            "resolution_notes": "fix_verified: deployed x; observed y",
        }
    ).model_dump_one_level()
    assert received["resolution_notes"]
    parsed = kg_handlers._parse_knowledge_update_request(
        {key: value for key, value in received.items() if value is not None}
    )
    assert parsed.discovery_id == "d-7"
    assert parsed.resolution_note == "fix_verified: deployed x; observed y"


def test_update_finding_on_mcp_carries_resolution_notes_but_not_the_class():
    """The premise both notes are written against, on the /mcp/ argument model.

    update_finding's wire schema does not declare closure_class or
    closure_evidence, and FastMCP discards undeclared arguments, so a direct
    /mcp/ update_finding(closure_class=...) never reaches the handler. The
    knowledge router declares both. When update_finding gains them, this test
    fails and the persisted note may name update_finding instead.
    """
    from src import mcp_server

    def dumped(tool_name: str, arguments: dict) -> dict:
        tool = mcp_server.mcp._tool_manager.get_tool(tool_name)
        return tool.fn_metadata.arg_model.model_validate(
            arguments
        ).model_dump_one_level()

    sent = {
        "discovery_id": "d-1",
        "status": "resolved",
        "resolution_notes": "fix_verified: deployed x; observed y",
        "closure_class": "fix_verified",
        "closure_evidence": {"deployed": "x", "observed": "y"},
    }
    alias = dumped("update_finding", sent)
    assert alias["resolution_notes"] == sent["resolution_notes"]
    assert "closure_class" not in alias and "closure_evidence" not in alias

    router = dumped("knowledge", {"action": "update", **sent})
    assert router["closure_class"] == "fix_verified"
    assert router["closure_evidence"] == sent["closure_evidence"]


def test_persisted_note_names_a_call_that_works_as_written():
    """Once classes are stored, the note's call must pass validation and /mcp/."""
    from src import mcp_server

    with patch.object(kg_handlers, "CLOSURE_CLASS_PERSISTED", True):
        note = _note("d-9", "resolved")

    call = re.search(
        r"(\w+)\(action='update', discovery_id='d-9', status='resolved', closure_class='\.\.\.'\)",
        note,
    )
    assert call, note
    tool = mcp_server.mcp._tool_manager.get_tool(call.group(1))

    derived = _derived_evidence_requirements()
    for closure_class, keys in derived.items():
        evidence = None
        if keys:
            shown = re.search(
                rf"'{closure_class}' also needs closure_evidence=(\{{[^}}]*\}})", note
            )
            assert shown, f"{closure_class} evidence not named: {note}"
            evidence = ast.literal_eval(shown.group(1))
            assert tuple(evidence) == keys
            evidence = {key: f"what the {key} was" for key in evidence}
        arguments = {
            "action": "update",
            "discovery_id": "d-9",
            "status": "resolved",
            "closure_class": closure_class,
        }
        if evidence is not None:
            arguments["closure_evidence"] = evidence
        received = tool.fn_metadata.arg_model.model_validate(
            arguments
        ).model_dump_one_level()
        _validate_closure_class(
            _request(
                closure_class=received["closure_class"],
                closure_evidence=received.get("closure_evidence"),
            ),
            received["status"],
        )


# ---------------------------------------------------------------------------
# Through the real handler
# ---------------------------------------------------------------------------


@pytest.fixture
def server():
    server = MagicMock()
    server.agent_metadata = {}
    server.monitors = {}
    return server


@pytest.fixture
def graph():
    graph = AsyncMock()
    graph.add_discovery = AsyncMock(return_value=True)
    graph.find_similar = AsyncMock(return_value=[])
    graph.update_discovery = AsyncMock(return_value=True)
    graph._get_db = AsyncMock()
    return graph


@pytest.fixture
def handler_env(server, graph):
    with (
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch("src.mcp_handlers.shared.get_mcp_server", return_value=server),
        patch("src.mcp_handlers.knowledge.handlers.mcp_server", server),
        patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            new_callable=AsyncMock,
            return_value=graph,
        ),
        patch("src.mcp_handlers.knowledge.handlers.record_ms"),
    ):
        yield server, graph


def _discovery(**overrides) -> DiscoveryNode:
    fields = dict(
        id="d-1",
        agent_id="a-1",
        type="bug_found",
        summary="s",
        details="body",
        severity="low",
        status="open",
    )
    fields.update(overrides)
    return DiscoveryNode(**fields)


@pytest.mark.asyncio
async def test_a_passed_class_is_not_acknowledged_as_recorded(handler_env):
    _server, graph = handler_env
    graph.get_discovery = AsyncMock(side_effect=[_discovery(), _discovery()])

    with patch.object(kg_handlers, "CLOSURE_CLASS_PERSISTED", False):
        data = parse_result(
            await kg_handlers.handle_update_discovery_status_graph(
                {
                    "agent_id": "a-1",
                    "discovery_id": "d-1",
                    "status": "resolved",
                    "closure_class": "duplicate",
                }
            )
        )

    assert data["success"] is True
    assert data["closure_class"] == "duplicate"
    assert "does not store closure_class" in data["closure_class_note"]
    assert "resolution_notes is stored" in data["closure_class_note"]


@pytest.mark.asyncio
async def test_a_stored_class_gets_no_caveat(handler_env):
    _server, graph = handler_env
    graph.get_discovery = AsyncMock(side_effect=[_discovery(), _discovery()])

    with patch.object(kg_handlers, "CLOSURE_CLASS_PERSISTED", True):
        data = parse_result(
            await kg_handlers.handle_update_discovery_status_graph(
                {
                    "agent_id": "a-1",
                    "discovery_id": "d-1",
                    "status": "resolved",
                    "closure_class": "duplicate",
                }
            )
        )

    assert data["closure_class"] == "duplicate"
    assert "closure_class_note" not in data


@pytest.mark.asyncio
async def test_unclassified_close_note_names_its_own_discovery_and_status(handler_env):
    _server, graph = handler_env
    graph.get_discovery = AsyncMock(side_effect=[_discovery(), _discovery()])

    with patch.object(kg_handlers, "CLOSURE_CLASS_PERSISTED", True):
        data = parse_result(
            await kg_handlers.handle_update_discovery_status_graph(
                {
                    "agent_id": "a-1",
                    "discovery_id": "d-1",
                    "status": "wont_fix",
                }
            )
        )

    assert "discovery_id='d-1', status='wont_fix'" in data["closure_class_note"]


def test_invalid_class_refusal_names_values_as_prose():
    with pytest.raises(_UpdateResponseError) as refused:
        _validate_closure_class(_request(closure_class="probably_fine"), "resolved")
    message = json.loads(refused.value.response.text)["error"]
    assert "[" not in message
    for closure_class in CLOSURE_CLASSES:
        assert f"'{closure_class}'" in message


# ---------------------------------------------------------------------------
# store_finding hints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_store_hint_names_how_to_bind(handler_env):
    """Retry with the id this process has, or mint one; never mint blindly."""
    data = parse_result(
        await kg_handlers.handle_store_knowledge_graph(
            {
                "summary": "an anonymous low-severity finding",
                "discovery_type": "note",
            }
        )
    )

    assert data["success"] is True
    assert data["agent_mode"] == "anonymous"
    hint = data["_identity_hint"]
    assert "client_session_id that start_session returned" in hint
    assert "start_session(force_new=true)" in hint


def test_display_name_hint_names_whose_label_to_set():
    """identity(name=...) alone resolves the caller from transport signals."""
    meta = MagicMock()
    meta.label = None
    meta.display_name = None
    fake_server = MagicMock()
    fake_server.agent_metadata = {"11111111-2222-4333-8444-555555555555": meta}
    with (
        patch("src.mcp_handlers.knowledge.handlers.mcp_server", fake_server),
        patch(
            "src.mcp_handlers.context.get_context_agent_id",
            return_value="11111111-2222-4333-8444-555555555555",
        ),
    ):
        _error, warning = kg_handlers._check_display_name_required(
            "11111111-2222-4333-8444-555555555555", {}
        )
    assert warning, "premise: an unnamed agent gets the hint"
    assert "identity(client_session_id='...', name='YourName')" in warning
