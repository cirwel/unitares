"""Fields an agent must carry out of a friendly response survive the envelope.

``build_experience_envelope`` rebuilds each friendly response from scratch
rather than filtering the canonical payload, so a field a handler mints can
vanish without anything failing. It happened with ``prediction_id`` (#2123,
fixed in #2124), found by running the quickstart, not by a test. #2125
audited every alias (docs/analysis/envelope-field-audit-2026-10-19.md) and
classified each dropped field as an intentional omission or a required
preserved one: a value the agent passes back to finish a documented loop, or
a documented caller signal that no later read returns.

REQUIRED below is that required set, per alias, with where the envelope puts
each field. Each case runs a payload through the alias's DEFAULT shape, which
omits raw_governance on every alias now, and asserts each field is reachable
outside raw_governance with the value the producer minted. A new experience
alias fails ``test_every_experience_alias_declares_its_required_fields`` until
someone decides what it must preserve.

Payloads come from the real producers where the suite has one (the onboard
handler, the metrics read, the check-in formatter, the lean search
projection); the knowledge writes, the outcome and the review are built in
their handlers' live shapes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Callable, Dict, List, Tuple

import pytest

from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_formatter import format_response
from tests.helpers import metrics_producer, onboard_producer

# alias -> {field: path in the envelope}. "*" maps over a list.
REQUIRED: Dict[str, Dict[str, Tuple[str, ...]]] = {
    # Every later call carries client_session_id; agent_uuid is the record;
    # the token is the documented same-process rebind proof.
    "start_session": {
        "uuid": ("agent_uuid",),
        "client_session_id": ("client_session_id",),
        "continuity_token": ("rebind", "continuity_token"),
    },
    # prediction_id binds record_result to this check-in (#2123). warnings
    # are this check-in's own, documented to be inspected, and no read
    # returns them later.
    "sync_state": {
        "prediction_id": ("prediction_id",),
        "warnings": ("warnings",),
    },
    # The strict-identity contract confirms threading by reading agent_uuid
    # off check_working_state(client_session_id=...).
    "check_working_state": {
        "agent_uuid": ("agent_uuid",),
    },
    # Each shown digest's id is what knowledge(action='details') takes; a
    # clamped limit, or imported memory left out of a listing, is the only
    # sign the result set is not the whole set.
    "search_shared_memory": {
        "discovery_id": ("memory_suggestions", "*", "discovery_id"),
        "limit_clamped_from": ("state_summary", "limit_clamped_from"),
        "imported_omitted": ("state_summary", "imported_omitted"),
        "imported_omitted_hint": ("state_summary", "imported_omitted_hint"),
    },
    "store_finding": {"discovery_id": ("discovery_id",)},
    "update_finding": {"discovery_id": ("discovery_id",)},
    "record_result": {"outcome_id": ("state_summary", "outcome_id")},
    # Every later dialectic call (get, thesis, antithesis) takes session_id.
    "request_review": {"session_id": ("state_summary", "session_id")},
}


def _at(obj: Any, path: Tuple[str, ...]) -> Any:
    for index, key in enumerate(path):
        if key == "*":
            assert isinstance(obj, list), (path, obj)
            return [_at(item, path[index + 1:]) for item in obj]
        assert isinstance(obj, dict) and key in obj, (path, key, obj)
        obj = obj[key]
    return obj


def _signature() -> Dict[str, Any]:
    return metrics_producer.agent_signature()


# --- payload builders: (canonical, envelope, expected {field: value}) --------


async def _start_session() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    arguments = {"force_new": True}
    payload = await onboard_producer.mint(arguments)
    env, _size = await onboard_producer.start_session(arguments, payload)
    return env, {
        "uuid": payload["uuid"],
        "client_session_id": payload["client_session_id"],
        "continuity_token": payload["continuity_token"],
    }


def _sync_source(**extra) -> Dict[str, Any]:
    source = {
        "success": True,
        "status": "healthy",
        "health_status": "healthy",
        "prediction_id": "64c5d92a-fe16-46a5-93f0-4e7c2848aa06",
        "decision": {
            "action": "proceed",
            "sub_action": "approve",
            "reason": "Low risk (27.0%) - healthy operating range",
            "margin": "comfortable",
            "nearest_edge": None,
            "unmeasurable_edges": ["coherence"],
            "margin_scope": "measured_edges_only",
        },
        "metrics": {
            "E": 0.72, "I": 0.79, "S": 0.21, "V": -0.02,
            "coherence": 0.48,
            "coherence_source": "legacy_tanh_v",
            "coherence_role": "ode_control_feedback",
            "risk_score": 0.27,
            "verdict": "safe",
            "health_status": "healthy",
        },
    }
    source.update(extra)
    return source


_WARNINGS = [
    "Identity assurance is weak (ip_ua_fingerprint); confidence dampened "
    "from 0.90 to 0.55."
]


def _sync_case(mode: str) -> Callable[[], Tuple[Dict[str, Any], Dict[str, Any]]]:
    def build():
        source = _sync_source(warnings=list(_WARNINGS))
        arguments = {"response_mode": mode}
        formatted = format_response(deepcopy(source), dict(arguments))
        env = build_experience_envelope(
            "sync_state", "process_agent_update", formatted, arguments
        )
        return env, {"prediction_id": source["prediction_id"], "warnings": _WARNINGS}
    return build


def _check_working_state():
    async def build():
        payload, validated = await metrics_producer.real_metrics_payload()
        env = build_experience_envelope(
            "check_working_state", "get_governance_metrics", payload, validated
        )
        return env, {"agent_uuid": metrics_producer.AGENT_UUID}
    return build


_IMPORTED_HINT = (
    "This listing left out unpromoted imported-memory rows. Pass "
    "authority_mode='all', or filter on an imported-memory tag, to read them; "
    "a query search still returns them, down-ranked."
)


def _search_full_payload(
    count: int, clamped_from: int | None, imported_omitted: int = 0
) -> Dict[str, Any]:
    payload = {
        "success": True,
        "query": "envelope drops",
        "count": count,
        "search_mode_used": "hybrid",
        "discoveries": [
            {
                "id": f"2026-10-19T00:00:0{i}.000000+00:00",
                "summary": f"Finding {i} about envelope field drops.",
                "type": "note",
                "status": "open",
                "tags": ["envelope"],
                "by": "Writer",
                "_agent_id": "writer-id",
            }
            for i in range(count)
        ],
        "agent_signature": _signature(),
    }
    if clamped_from is not None:
        payload["limit_clamped_from"] = clamped_from
        payload["_more_available"] = (
            f"You asked for {clamped_from} results; the server maximum is "
            f"{count}, so results were truncated there."
        )
    if imported_omitted:
        payload["imported_omitted"] = imported_omitted
        payload["imported_omitted_hint"] = _IMPORTED_HINT
    return payload


def _search_shared_memory():
    from src.mcp_handlers.knowledge.handlers import _lean_search_payload

    full = _search_full_payload(5, clamped_from=150, imported_omitted=2)
    lean = _lean_search_payload(deepcopy(full))
    env = build_experience_envelope("search_shared_memory", "knowledge", lean, {})
    shown = len(env["memory_suggestions"])
    return env, {
        "discovery_id": [row["id"] for row in full["discoveries"][:shown]],
        "limit_clamped_from": 150,
        "imported_omitted": 2,
        "imported_omitted_hint": _IMPORTED_HINT,
    }


_DISCOVERY_ID = "2026-10-19T01:02:03.456789+00:00"


def _store_finding():
    payload = {
        "success": True,
        "message": "Discovery stored",
        "discovery_id": _DISCOVERY_ID,
        "discovery": {"id": _DISCOVERY_ID, "type": "note", "status": "open",
                      "summary": "A finding."},
        "agent_signature": _signature(),
    }
    env = build_experience_envelope(
        "store_finding", "knowledge", payload, {"action": "store"}
    )
    return env, {"discovery_id": _DISCOVERY_ID}


def _update_finding():
    payload = {
        "success": True,
        "message": "Discovery updated",
        "discovery_id": _DISCOVERY_ID,
        "discovery": {"id": _DISCOVERY_ID, "type": "note", "status": "resolved",
                      "summary": "A finding."},
        "agent_signature": _signature(),
    }
    env = build_experience_envelope(
        "update_finding", "knowledge", payload,
        {"action": "update", "discovery_id": _DISCOVERY_ID},
    )
    return env, {"discovery_id": _DISCOVERY_ID}


def _record_result():
    payload = {
        "success": True,
        "outcome_id": "5f2a9c1e-0b1d-4e3f-8a7b-6c5d4e3f2a1b",
        "outcome_type": "test_passed",
        "outcome_score": 1.0,
        "is_bad": False,
        "prediction_binding": "registry",
        "prediction_source": "prediction_id",
        "calibration_excluded": False,
        "idempotent_replay": False,
        "agent_signature": _signature(),
    }
    env = build_experience_envelope("record_result", "outcome_event", payload, {})
    return env, {"outcome_id": payload["outcome_id"]}


def _request_review():
    payload = {
        "success": True,
        "server_time": "2026-10-19T00:00:00+00:00",
        "phase": "antithesis",
        "session_id": "8f1c2d3e4a5b6c7d",
        "next_step": "An eligible reviewer should claim the session by submitting antithesis",
        "one_call_review": True,
        "thesis_source": "issue_description",
        "whose_move": "a reviewer's — thesis recorded, awaiting a reviewer to claim the open slot",
        "agent_signature": _signature(),
    }
    env = build_experience_envelope("request_review", "dialectic", payload, {})
    return env, {"session_id": payload["session_id"]}


_CASES: List[Tuple[str, str, Callable]] = [
    ("start_session", "default mint", _start_session),
    ("sync_state", "auto (warnings expand it to mirror)", _sync_case("auto")),
    ("sync_state", "explicit compact", _sync_case("compact")),
    ("check_working_state", "minimal", _check_working_state()),
    ("search_shared_memory", "lean, clamped and imported omitted", _search_shared_memory),
    ("store_finding", "ack", _store_finding),
    ("update_finding", "ack", _update_finding),
    ("record_result", "ack", _record_result),
    ("request_review", "routine one-call", _request_review),
]


def test_every_experience_alias_declares_its_required_fields():
    from src.mcp_handlers.tool_stability import list_all_aliases

    experience = {
        name for name, alias in list_all_aliases().items() if alias.experience
    }
    assert experience == set(REQUIRED), (
        "Declare what each experience alias must preserve (or that it needs "
        "nothing) in REQUIRED, and add a case below."
    )
    assert {alias for alias, _, _ in _CASES} == set(REQUIRED)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias,case,build", _CASES, ids=[f"{a}: {c}" for a, c, _ in _CASES]
)
async def test_required_fields_survive_the_default_envelope(alias, case, build):
    built = build()
    if hasattr(built, "__await__"):
        built = await built
    env, expected = built
    assert env["tool"] == alias
    # Every alias's default shape is the bounded one, so this is the case
    # where a dropped field has nowhere else to be.
    assert "raw_governance" not in env, case
    assert set(expected) == set(REQUIRED[alias])
    for field, path in REQUIRED[alias].items():
        value = _at(env, path)
        assert value == expected[field], (alias, field, path, value)
        assert value not in (None, [], ""), (alias, field)


# --- per-field regressions for the drops #2125 fixed -------------------------


def test_sync_state_warnings_force_the_full_shape_on_a_compact_proceed():
    """A clean proceed that carries a warning is not routine: the warning is
    the formatter's own reason to expand auto to mirror."""
    source = _sync_source(warnings=list(_WARNINGS))
    arguments = {"response_mode": "compact"}
    formatted = format_response(deepcopy(source), dict(arguments))
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, arguments
    )
    assert env["warnings"] == _WARNINGS
    assert env.get("response_shape") != "routine"


def test_a_sync_state_without_warnings_carries_no_warnings_key():
    source = _sync_source()
    arguments = {"response_mode": "auto"}
    formatted = format_response(deepcopy(source), dict(arguments))
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, arguments
    )
    assert "warnings" not in env
    assert env.get("response_shape") == "routine"


def test_full_mode_sync_state_keeps_warnings_in_raw_only():
    source = _sync_source(warnings=list(_WARNINGS))
    arguments = {"response_mode": "full"}
    formatted = format_response(deepcopy(source), dict(arguments))
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, arguments
    )
    assert env["raw_governance"]["warnings"] == _WARNINGS
    assert "warnings" not in env


def test_an_unclamped_search_carries_no_limit_clamped_from():
    from src.mcp_handlers.knowledge.handlers import _lean_search_payload

    lean = _lean_search_payload(_search_full_payload(5, clamped_from=None))
    env = build_experience_envelope("search_shared_memory", "knowledge", lean, {})
    assert "limit_clamped_from" not in env["state_summary"]


def test_a_clamped_search_says_so_within_the_lean_budget():
    from src.mcp_handlers.knowledge.handlers import _lean_search_payload

    lean = _lean_search_payload(_search_full_payload(5, clamped_from=500))
    env = build_experience_envelope("search_shared_memory", "knowledge", lean, {})
    assert env["state_summary"]["limit_clamped_from"] == 500
    assert "_response_size" not in env, json.dumps(env)[:400]


def test_the_lean_projection_keeps_the_imported_memory_omission():
    """The lean projection dropped imported_omitted and its hint, so the
    envelope's lift of them was dead on the friendly default."""
    from src.mcp_handlers.knowledge.handlers import _lean_search_payload

    lean = _lean_search_payload(
        _search_full_payload(3, clamped_from=None, imported_omitted=4)
    )
    assert lean["imported_omitted"] == 4
    assert lean["imported_omitted_hint"] == _IMPORTED_HINT
    env = build_experience_envelope("search_shared_memory", "knowledge", lean, {})
    assert env["state_summary"]["imported_omitted"] == 4
    assert "authority_mode='all'" in env["state_summary"]["imported_omitted_hint"]
    assert "_response_size" not in env, json.dumps(env)[:400]


def test_a_listing_with_nothing_omitted_carries_no_omission_keys():
    from src.mcp_handlers.knowledge.handlers import _lean_search_payload

    lean = _lean_search_payload(_search_full_payload(3, clamped_from=None))
    env = build_experience_envelope("search_shared_memory", "knowledge", lean, {})
    assert "imported_omitted" not in env["state_summary"]
    assert "imported_omitted_hint" not in env["state_summary"]
