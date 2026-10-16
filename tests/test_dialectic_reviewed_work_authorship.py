"""Declared work authors cannot become independent dialectic reviewers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.dialectic_protocol import (
    DialecticMessage,
    DialecticPhase,
    DialecticSession,
)
from src.dialectic_reviewed_work import (
    REVIEWED_WORK_AUTHORSHIP_STATE_KEY,
    ReviewedWorkAuthorship,
    ReviewedWorkAuthorshipError,
    declaration_for_request,
    embed_reviewed_work_authorship,
    parse_reviewed_work_author_agent_ids,
    read_reviewed_work_authorship,
)


PROXY = "11111111-1111-4111-8111-111111111111"
AUTHOR = "22222222-2222-4222-8222-222222222222"
PEER = "33333333-3333-4333-8333-333333333333"


class _AsyncpgRecordLike:
    """Mapping protocol shape exposed by asyncpg.Record without ABC registration."""

    def __init__(self, values: dict):
        self._values = values

    def items(self):
        return self._values.items()


def _state(*authors: str) -> dict:
    declaration = declaration_for_request(
        list(authors), declared_by_agent_id=PROXY
    )
    return embed_reviewed_work_authorship(
        {"schema": "unitares.pause-evidence.v1"}, declaration
    )


def _message(agent_id: str, *, phase: str = "antithesis") -> DialecticMessage:
    return DialecticMessage(
        phase=phase,
        agent_id=agent_id,
        timestamp="2026-10-15T00:00:00+00:00",
        reasoning="Independent assessment",
        concerns=["conflict"],
        agrees=True if phase == "synthesis" else None,
    )


def test_request_ids_are_uuid_only_bounded_and_stably_deduplicated():
    assert parse_reviewed_work_author_agent_ids([AUTHOR, AUTHOR, PEER]) == [
        AUTHOR,
        PEER,
    ]

    with pytest.raises(ReviewedWorkAuthorshipError):
        parse_reviewed_work_author_agent_ids([])
    with pytest.raises(ReviewedWorkAuthorshipError):
        parse_reviewed_work_author_agent_ids(["author-label"])
    with pytest.raises(ReviewedWorkAuthorshipError):
        parse_reviewed_work_author_agent_ids(
            ["AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"]
        )
    with pytest.raises(ReviewedWorkAuthorshipError):
        parse_reviewed_work_author_agent_ids([
            f"00000000-0000-4000-8000-{index:012d}"
            for index in range(33)
        ])


def test_declaration_round_trips_and_legacy_absence_is_disclosed():
    state = _state(AUTHOR)
    declaration = read_reviewed_work_authorship(json.dumps(state))

    assert declaration.status == "declared"
    assert declaration.author_agent_ids == (AUTHOR,)
    assert declaration.declared_by_agent_id == PROXY
    assert declaration.as_dict()["authorship_verified"] is False
    assert read_reviewed_work_authorship({}).status == "not_supplied"


def test_present_malformed_reserved_marker_fails_closed():
    marker = _state(AUTHOR)[REVIEWED_WORK_AUTHORSHIP_STATE_KEY]
    marker.pop("reviewed_work_author_agent_ids")
    declaration = read_reviewed_work_authorship(
        {REVIEWED_WORK_AUTHORSHIP_STATE_KEY: marker}
    )

    assert declaration.status == "invalid"
    assert declaration.recuses(PEER, paused_agent_id=PROXY) is True


def test_asyncpg_record_like_session_row_preserves_declared_authorship():
    declaration = read_reviewed_work_authorship(_AsyncpgRecordLike({
        "paused_agent_id": PROXY,
        "paused_agent_state_json": _state(AUTHOR),
    }))

    assert declaration.status == "declared"
    assert declaration.author_agent_ids == (AUTHOR,)
    assert declaration.recuses(AUTHOR, paused_agent_id=PROXY) is True


def test_omitted_declaration_clears_any_inherited_reserved_marker():
    inherited = {
        REVIEWED_WORK_AUTHORSHIP_STATE_KEY: {
            "status": "declared",
            "reviewed_work_author_agent_ids": [AUTHOR],
        }
    }

    state = embed_reviewed_work_authorship(
        inherited,
        declaration_for_request([], declared_by_agent_id=PROXY),
    )

    assert REVIEWED_WORK_AUTHORSHIP_STATE_KEY not in state
    assert read_reviewed_work_authorship(state).status == "not_supplied"


def test_server_embedding_clears_public_projection_and_reserved_marker_wins():
    stale_public = ReviewedWorkAuthorship(status="not_supplied").as_dict()
    state = embed_reviewed_work_authorship(
        {"reviewed_work_authorship": stale_public},
        declaration_for_request([AUTHOR], declared_by_agent_id=PROXY),
    )

    assert "reviewed_work_authorship" not in state
    record = {
        "paused_agent_state": state,
        "reviewed_work_authorship": stale_public,
    }
    assert read_reviewed_work_authorship(record).author_agent_ids == (AUTHOR,)


def test_duplicate_ids_in_stored_marker_fail_closed():
    state = _state(AUTHOR)
    state[REVIEWED_WORK_AUTHORSHIP_STATE_KEY][
        "reviewed_work_author_agent_ids"
    ] = [AUTHOR, AUTHOR]

    declaration = read_reviewed_work_authorship(state)

    assert declaration.status == "invalid"
    assert declaration.error == "duplicate_author_agent_id"
    assert declaration.recuses(PEER, paused_agent_id=PROXY) is True


def test_declarer_must_match_the_session_requester():
    state = _state(AUTHOR)

    declaration = read_reviewed_work_authorship(
        state, expected_declared_by_agent_id=PEER
    )

    assert declaration.status == "invalid"
    assert declaration.error == "declarer_session_mismatch"


def test_protocol_refuses_author_before_open_slot_mutation():
    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=None,
        paused_agent_state=_state(AUTHOR),
    )
    session.phase = DialecticPhase.ANTITHESIS

    result = session.submit_antithesis(_message(AUTHOR))

    assert result["success"] is False
    assert result["error_code"] == "REVIEWER_AUTHOR_CONFLICT"
    assert session.reviewer_agent_id is None
    assert session.transcript == []


def test_protocol_refuses_already_assigned_author_and_synthesis():
    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=AUTHOR,
        paused_agent_state=_state(AUTHOR),
    )
    session.phase = DialecticPhase.ANTITHESIS
    assert session.submit_antithesis(_message(AUTHOR))["success"] is False

    session.phase = DialecticPhase.SYNTHESIS
    result = session.submit_synthesis(_message(PROXY, phase="synthesis"))
    assert result["success"] is False
    assert result["blocked"] == "reviewer_author_conflict"


def test_actionability_does_not_invite_author_to_claim_open_slot():
    from src.mcp_handlers.dialectic.handlers import _build_dialectic_actionability

    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=None,
        paused_agent_state=_state(AUTHOR),
    )
    session.phase = DialecticPhase.ANTITHESIS

    with patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=AUTHOR
    ), patch(
        "src.mcp_handlers.identity.operator.is_operator_caller", return_value=False
    ):
        actionability = _build_dialectic_actionability(session.to_dict())

    assert actionability["current_agent_role"] == "reviewed_work_author"
    assert actionability["current_agent_can_submit"] is False
    assert actionability["next_call"] is None
    assert actionability["reviewed_work_authorship"]["status"] == "declared"
    assert "NOT YOURS" in actionability["whose_move"]


@pytest.mark.asyncio
async def test_proxy_request_excludes_author_and_persists_declaration():
    from src.mcp_handlers.dialectic.handlers import (
        handle_request_dialectic_review,
    )

    meta = MagicMock(status="active", tags=[])
    server = MagicMock()
    server.agent_metadata = {PROXY: meta, AUTHOR: meta, PEER: meta}
    server.load_metadata_async = AsyncMock()

    with patch(
        "src.mcp_handlers.dialectic.handlers.mcp_server", server
    ), patch(
        "src.mcp_handlers.dialectic.handlers.require_registered_agent",
        return_value=(PROXY, None),
    ), patch(
        "src.mcp_handlers.utils.verify_agent_ownership", return_value=True
    ), patch(
        "src.mcp_handlers.dialectic.handlers.is_agent_in_active_session",
        new=AsyncMock(return_value=False),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.select_reviewer",
        new=AsyncMock(return_value=PEER),
    ) as select_reviewer, patch(
        "src.mcp_handlers.dialectic.handlers.beam_create_session",
        new=AsyncMock(return_value=None),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.pg_create_session",
        new=AsyncMock(),
    ) as create_session, patch(
        "src.mcp_handlers.dialectic.handlers._emit_dialectic_event",
        new=AsyncMock(),
    ), patch(
        "src.agent_storage.read_authoritative_pause_binding",
        new=AsyncMock(return_value={
            "pause_effect_required": False,
            "pause_token": None,
            "status": "active",
        }),
    ):
        response = await handle_request_dialectic_review({
            "agent_id": PROXY,
            "_agent_uuid": PROXY,
            "reason": "Review the author's change",
            "reviewed_work_author_agent_ids": [AUTHOR],
        })

    payload = json.loads(response[0].text)
    assert payload["success"] is True
    assert payload["reviewer_agent_id"] == PEER
    assert payload["reviewed_work_authorship"]["status"] == "declared"
    assert AUTHOR in select_reviewer.await_args.kwargs["exclude_agent_ids"]
    stored = create_session.await_args.kwargs["paused_agent_state"]
    assert read_reviewed_work_authorship(stored).author_agent_ids == (AUTHOR,)


@pytest.mark.asyncio
async def test_request_rejects_unregistered_author_uuid():
    from src.mcp_handlers.dialectic.handlers import (
        _requested_reviewed_work_authorship,
    )

    server = MagicMock()
    server.agent_metadata = {PROXY: MagicMock(status="active")}
    server.load_metadata_async = AsyncMock()

    with patch("src.mcp_handlers.dialectic.handlers.mcp_server", server):
        declaration, error = await _requested_reviewed_work_authorship(
            {"reviewed_work_author_agent_ids": [AUTHOR]},
            declared_by_agent_id=PROXY,
        )

    assert declaration is None
    payload = json.loads(error[0].text)
    assert payload["error_code"] == "REVIEW_AUTHOR_NOT_FOUND"
    assert payload["recovery"]["unregistered_agent_ids"] == [AUTHOR]


@pytest.mark.asyncio
async def test_fast_session_read_discloses_authorship_declaration():
    from src.mcp_handlers.dialectic.session import load_session_as_dict

    now = datetime.now(timezone.utc)
    row = {
        "session_id": "fast-authorship",
        "phase": "antithesis",
        "status": "active",
        "session_type": "review",
        "paused_agent_id": PROXY,
        "reviewer_agent_id": PEER,
        "topic": "Review proxy-authored work",
        "created_at": now,
        "resolution_json": None,
        "reason": "Independent review",
        "trigger_source": "manual",
        "awaiting_facilitation": False,
        "synthesis_round": 0,
        "max_synthesis_rounds": 5,
        "paused_agent_state_json": _state(AUTHOR),
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=[])
    pool = AsyncMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    db = AsyncMock()
    db._pool = pool
    db._ensure_pool = AsyncMock()

    with patch(
        "src.dialectic_db.get_dialectic_db",
        new=AsyncMock(return_value=db),
    ):
        result = await load_session_as_dict("fast-authorship")

    assert result["reviewed_work_authorship"]["status"] == "declared"
    assert result["reviewed_work_authorship"][
        "reviewed_work_author_agent_ids"
    ] == [AUTHOR]
    assert "paused_agent_state_json" in conn.fetchrow.await_args.args[0]


@pytest.mark.asyncio
async def test_session_list_discloses_authorship_declaration():
    from src.mcp_handlers.dialectic.session import list_all_sessions

    now = datetime.now(timezone.utc)
    row = {
        "session_id": "list-authorship",
        "phase": "antithesis",
        "status": "active",
        "session_type": "review",
        "paused_agent_id": PROXY,
        "reviewer_agent_id": PEER,
        "topic": "Review proxy-authored work",
        "created_at": now,
        "resolution_json": None,
        "awaiting_facilitation": False,
        "synthesis_round": 0,
        "max_synthesis_rounds": 5,
        "paused_agent_label": "proxy",
        "message_count": 0,
        "synthesizer": None,
        "paused_agent_state_json": _state(AUTHOR),
    }
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[row])
    pool = AsyncMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    db = AsyncMock()
    db._pool = pool
    db._ensure_pool = AsyncMock()

    with patch(
        "src.dialectic_db.get_dialectic_db",
        new=AsyncMock(return_value=db),
    ):
        result = await list_all_sessions()

    assert result[0]["reviewed_work_authorship"]["status"] == "declared"
    assert result[0]["reviewed_work_authorship"][
        "reviewed_work_author_agent_ids"
    ] == [AUTHOR]
    assert "paused_agent_state_json" in conn.fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_public_fast_get_identifies_author_and_offers_no_write_call():
    from src.mcp_handlers.dialectic.handlers import handle_get_dialectic_session

    row = {
        "session_id": "fast-authorship",
        "phase": "antithesis",
        "status": "active",
        "paused_agent": PROXY,
        "reviewer": PEER,
        "awaiting_facilitation": False,
        "transcript": [],
        "reviewed_work_authorship": read_reviewed_work_authorship(
            _state(AUTHOR)
        ).as_dict(),
    }

    with patch(
        "src.mcp_handlers.dialectic.handlers.load_session_as_dict",
        new=AsyncMock(return_value=row),
    ), patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=AUTHOR
    ), patch(
        "src.mcp_handlers.identity.operator.is_operator_caller", return_value=False
    ):
        response = await handle_get_dialectic_session({
            "session_id": "fast-authorship"
        })

    payload = json.loads(response[0].text)
    assert payload["current_agent_role"] == "reviewed_work_author"
    assert payload["current_agent_can_submit"] is False
    assert payload["next_call"] is None


@pytest.mark.asyncio
async def test_recused_reviewer_is_immediately_stuck_and_cannot_hand_off():
    from src.mcp_handlers.dialectic.handlers import (
        _build_dialectic_actionability,
        _reviewer_reassignment_actor,
        check_reviewer_stuck,
    )

    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=AUTHOR,
        paused_agent_state=_state(AUTHOR),
    )
    session.phase = DialecticPhase.ANTITHESIS

    with patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=AUTHOR
    ), patch(
        "src.mcp_handlers.identity.operator.is_operator_caller", return_value=False
    ):
        assert await check_reviewer_stuck(session) is True
        assert _reviewer_reassignment_actor(session) is None

    session.phase = DialecticPhase.FAILED
    session.awaiting_facilitation = True
    with patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=AUTHOR
    ), patch(
        "src.mcp_handlers.identity.operator.is_operator_caller", return_value=False
    ):
        actionability = _build_dialectic_actionability(session.to_dict())
    assert actionability["current_agent_can_submit"] is False
    assert AUTHOR not in actionability["allowed_agent_ids"]
    assert "operator" in actionability["whose_move"]


@pytest.mark.asyncio
async def test_first_responder_author_is_rejected_before_any_write():
    from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis

    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=None,
        paused_agent_state=_state(AUTHOR),
    )
    session.phase = DialecticPhase.ANTITHESIS

    with patch(
        "src.mcp_handlers.dialectic.handlers._resolve_dialectic_agent_id",
        new=AsyncMock(return_value=(AUTHOR, None)),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.load_session",
        new=AsyncMock(return_value=session),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.pg_add_message",
        new=AsyncMock(),
    ) as add_message, patch(
        "src.mcp_handlers.dialectic.handlers.pg_update_reviewer",
        new=AsyncMock(),
    ) as update_reviewer:
        response = await handle_submit_antithesis({
            "session_id": session.session_id,
            "reasoning": "I approve my own work",
            "concerns": [],
            "observed_metrics": {},
        })

    payload = json.loads(response[0].text)
    assert payload["success"] is False
    assert payload["error_code"] == "REVIEWER_AUTHOR_CONFLICT"
    assert session.reviewer_agent_id is None
    add_message.assert_not_awaited()
    update_reviewer.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_responder_effect_owner_refusal_aborts_all_later_writes():
    from src.mcp_handlers.dialectic.handlers import handle_submit_antithesis

    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=None,
        paused_agent_state=_state(AUTHOR),
    )
    session.phase = DialecticPhase.ANTITHESIS

    with patch(
        "src.mcp_handlers.dialectic.handlers._resolve_dialectic_agent_id",
        new=AsyncMock(return_value=(PEER, None)),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.load_session",
        new=AsyncMock(return_value=session),
    ), patch(
        "src.mcp_handlers.dialectic.reviewer.is_agent_in_active_session",
        new=AsyncMock(return_value=False),
    ), patch(
        "src.mcp_handlers.dialectic.reviewer._has_recently_reviewed",
        new=AsyncMock(return_value=False),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.beam_update_reviewer",
        new=AsyncMock(return_value=None),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.pg_update_reviewer",
        new=AsyncMock(return_value=False),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.pg_add_message",
        new=AsyncMock(),
    ) as add_message, patch(
        "src.mcp_handlers.dialectic.handlers.pg_update_phase",
        new=AsyncMock(),
    ) as update_phase, patch(
        "src.mcp_handlers.dialectic.handlers.save_session",
        new=AsyncMock(),
    ) as save_session:
        response = await handle_submit_antithesis({
            "session_id": session.session_id,
            "reasoning": "Independent concern",
            "concerns": ["conflict"],
        })

    payload = json.loads(response[0].text)
    assert payload["success"] is False
    assert payload["error_code"] == "REVIEWER_ASSIGNMENT_REFUSED"
    assert session.reviewer_agent_id is None
    assert session.phase == DialecticPhase.ANTITHESIS
    assert session.transcript == []
    add_message.assert_not_awaited()
    update_phase.assert_not_awaited()
    save_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("judgment_formed", [False, True])
async def test_recused_reviewer_cannot_submit_or_abstain_in_synthesis(
    judgment_formed,
):
    from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=AUTHOR,
        paused_agent_state=_state(AUTHOR),
    )
    session.phase = DialecticPhase.SYNTHESIS

    with patch(
        "src.mcp_handlers.dialectic.handlers._resolve_dialectic_agent_id",
        new=AsyncMock(return_value=(AUTHOR, None)),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.load_session",
        new=AsyncMock(return_value=session),
    ), patch(
        "src.mcp_handlers.dialectic.handlers.emit_reviewer_abstained",
        new=AsyncMock(),
    ) as abstained, patch(
        "src.mcp_handlers.dialectic.handlers.pg_add_message",
        new=AsyncMock(),
    ) as add_message, patch(
        "src.mcp_handlers.dialectic.handlers.save_session",
        new=AsyncMock(),
    ) as save_session:
        response = await handle_submit_synthesis({
            "session_id": session.session_id,
            "judgment_formed": judgment_formed,
            "agrees": True,
            "proposed_conditions": ["condition"],
            "root_cause": "cause",
            "reasoning": "verdict",
        })

    payload = json.loads(response[0].text)
    assert payload["success"] is False
    assert payload["error_code"] == "REVIEWER_AUTHOR_CONFLICT"
    assert session.transcript == []
    abstained.assert_not_awaited()
    add_message.assert_not_awaited()
    save_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_transition_owner_refuses_declared_author():
    from src.mcp_handlers.dialectic import resolution as resolution_module

    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=AUTHOR,
        paused_agent_state=_state(AUTHOR),
    )
    resolution = MagicMock(
        action="resume",
        conditions=[],
        root_cause="cause",
    )
    meta = MagicMock(status="paused")
    server = MagicMock()
    server.agent_metadata = {PROXY: meta}
    server.load_metadata_async = AsyncMock()

    with patch.object(resolution_module, "mcp_server", server):
        result = await resolution_module.execute_resolution(session, resolution)

    assert result["success"] is False
    assert result["refused"] == "reviewer_author_conflict"
    assert meta.status == "paused"


@pytest.mark.asyncio
async def test_beam_reassignment_policy_refusal_rolls_back_memory():
    from src.mcp_handlers.dialectic.beam_resolve_client import (
        BeamDialecticPolicyRefusal,
    )
    from src.mcp_handlers.dialectic.handlers import (
        _apply_reviewer_reassignment,
    )

    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=PEER,
        paused_agent_state={},
    )
    session.phase = DialecticPhase.ANTITHESIS

    with patch(
        "src.mcp_handlers.dialectic.handlers.beam_update_reviewer",
        new=AsyncMock(
            side_effect=BeamDialecticPolicyRefusal(
                "session_identity_mismatch", operation="update_reviewer"
            )
        ),
    ):
        with pytest.raises(BeamDialecticPolicyRefusal):
            await _apply_reviewer_reassignment(
                session.session_id,
                session,
                AUTHOR,
                reason="test stale effect owner",
            )

    assert session.reviewer_agent_id == PEER
    assert session.phase == DialecticPhase.ANTITHESIS
    assert session.transcript == []


@pytest.mark.asyncio
async def test_declared_author_verdict_never_updates_peer_calibration():
    from src.mcp_handlers.dialectic import calibration as calibration_module

    session = DialecticSession(
        paused_agent_id=PROXY,
        reviewer_agent_id=AUTHOR,
        paused_agent_state=_state(AUTHOR),
        dispute_type="verification",
    )
    resolution = MagicMock(action="resume", timestamp=None)
    query = MagicMock(return_value=[])
    record = MagicMock()

    with patch.object(calibration_module.audit_logger, "query_audit_log", query), patch.object(
        calibration_module.calibration_checker, "record_prediction", record
    ):
        updated = await calibration_module.update_calibration_from_dialectic(
            session, resolution
        )

    assert updated is False
    query.assert_not_called()
    record.assert_not_called()
