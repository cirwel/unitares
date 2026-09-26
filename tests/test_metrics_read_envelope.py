"""The default check_working_state envelope says each fact once, and every
size or next-step hint an envelope emits resolves.

Built from real producer output (tests/helpers/metrics_producer.py and the
real response formatter), because the gaps pinned here survived behind
hand-built payloads: the default envelope was 3.2 KB live with about 43% of
it restated, include_state doubled it, and hints named paths the response did
not contain or the mode already in effect (external agent report,
2026-09-25).

A hint resolves when it names a field present in the returned object, or a
read-only call in a DIFFERENT mode; never the mode in effect, a new mint, or a
repeated write.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy

import pytest
from mcp.types import TextContent

from src.mcp_handlers.middleware.envelope_step import (
    _attach_response_size,
    apply_experience_envelope,
    build_experience_envelope,
)
from src.mcp_handlers.middleware import DispatchContext
from src.mcp_handlers.response_formatter import format_response
from src.mcp_handlers.support.param_normalization import resolve_metrics_verbosity
from tests.helpers.metrics_producer import agent_signature, real_metrics_payload

# A default read, provisional verdict (two check-ins), measured 1,986 B on the
# real producer; it was 3,350 B before this budget existed.
DEFAULT_METRICS_READ_BUDGET = 2_100

_SEE = re.compile(r" See ([a-z_.]+) for provenance\.")


def _wire(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


async def _metrics_envelope(arguments=None, *, check_ins=2, **kwargs):
    payload, validated = await real_metrics_payload(arguments, check_ins=check_ins)
    return build_experience_envelope(
        "check_working_state", "get_governance_metrics", payload, validated, **kwargs
    )


def _resolve(env: dict, dotted: str):
    node = env
    for key in dotted.split("."):
        assert isinstance(node, dict) and key in node, f"{dotted!r} not in response"
        node = node[key]
    return node


# ---------------------------------------------------------------------------
# Each fact once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("check_ins", [0, 2, 30])
async def test_default_read_says_each_fact_once(check_ins):
    env = await _metrics_envelope({}, check_ins=check_ins)
    state = env["state_summary"]

    # E/I/S/V and risk are bare values; their per-field contract rides once on
    # the tool description, the risk band in risk_summary.
    for key in ("E", "I", "S", "V"):
        assert isinstance(state[key], (int, float)), key
    assert not isinstance(state.get("risk_score"), dict)

    # Legacy coherence keeps its inline badge (#1872) in sync_state's shape,
    # and the separate legacy_diagnostics block that repeated it is gone.
    assert set(state["coherence"]) == {"value", "status", "source", "role"}
    assert state["coherence"]["role"] == "ode_control_feedback"
    if check_ins:
        assert "not health-rated" in state["coherence"]["status"]
    assert "legacy_diagnostics" not in env

    # The verdict's meaning is not repeated as the action reason.
    assert env["action_summary"].get("reason") != state.get("meaning")
    # One tier ladder, not two.
    assert ("response_options" in env) + ("raw_governance_hint" in env) == 1
    # The lifecycle contract's next_action, said once.
    assert env["next_action"]
    assert state.get("next_action") != env["next_action"]


@pytest.mark.asyncio
async def test_default_read_stays_inside_its_budget_and_under_standard():
    minimal = await _metrics_envelope({})
    standard = await _metrics_envelope({"verbosity": "standard"})
    assert _wire(minimal) <= DEFAULT_METRICS_READ_BUDGET
    assert _wire(minimal) <= _wire(standard)


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", [{}, {"verbosity": "standard"}, {"verbosity": "full"}])
async def test_include_state_changes_nothing(tier):
    """include_state never added state (runtime_queries overwrites the
    monitor's dict before any tier is built) and no longer forces the raw
    payload, so it is a no-op on the handler and on the envelope."""
    plain_payload, _ = await real_metrics_payload(tier)
    with_state_payload, _ = await real_metrics_payload({**tier, "include_state": True})
    assert set(with_state_payload) == set(plain_payload)

    plain = await _metrics_envelope(tier)
    with_state = await _metrics_envelope({**tier, "include_state": True})
    assert set(with_state) == set(plain)
    assert abs(_wire(with_state) - _wire(plain)) < 64


# ---------------------------------------------------------------------------
# Hints resolve
# ---------------------------------------------------------------------------


def _sync_source(check_ins: int) -> dict:
    """A real monitor check-in result, the base of process_agent_update's
    full-mode payload."""
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor(f"hint-source-{check_ins}", load_state=False)
    result = None
    for step in range(check_ins):
        result = monitor.process_update(
            {"response_text": f"step {step}: edited files", "complexity": 0.4},
            confidence=0.8,
            task_type="mixed",
        )
    return {"success": True, **result}


_SYNC_MODES = ("auto", "compact", "mirror", "standard", "minimal", "full")


def _assert_caveat_pointer_resolves(env: dict) -> None:
    caveat = env.get("verdict_caveat")
    if not caveat:
        return
    match = _SEE.search(caveat)
    if match is None:
        assert "raw_governance" not in caveat
        return
    _resolve(env, match.group(1))


@pytest.mark.asyncio
@pytest.mark.parametrize("check_ins", [0, 1, 2])
@pytest.mark.parametrize(
    "arguments", [{}, {"verbosity": "standard"}, {"verbosity": "full"}]
)
async def test_metrics_verdict_caveat_names_a_path_in_the_response(check_ins, arguments):
    env = await _metrics_envelope(arguments, check_ins=check_ins)
    assert "verdict_caveat" in env  # all three are provisional
    _assert_caveat_pointer_resolves(env)
    if "raw_governance" not in env:
        # The default read carries the evidence itself.
        if "evidence" in env["state_summary"]:
            assert "See state_summary.evidence" in env["verdict_caveat"]


@pytest.mark.parametrize("check_ins", [1, 3, 30])
@pytest.mark.parametrize("mode", _SYNC_MODES)
def test_sync_verdict_caveat_never_points_into_an_absent_payload(check_ins, mode):
    """A bounded check-in has no evidence object and no raw_governance, and
    re-calling sync_state to fetch one would write another check-in, so its
    caveat states the basis inline and names no path."""
    formatted = format_response(
        deepcopy(_sync_source(check_ins)), {"response_mode": mode}, task_type="mixed"
    )
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": mode}
    )
    _assert_caveat_pointer_resolves(env)


def _names_mode(text: str, mode: str) -> bool:
    return f"'{mode}'" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"lite": True},
        {"include_state": True},
        {"verbosity": "standard"},
        {"verbosity": "standard", "include_state": True},
        {"verbosity": "full"},
        {"lite": False},
    ],
)
async def test_metrics_hints_never_name_the_tier_in_effect(arguments):
    env = await _metrics_envelope(arguments)
    _, validated = await real_metrics_payload(arguments)
    current = resolve_metrics_verbosity(validated)
    for text in (
        env.get("raw_governance_hint") or "",
        env.get("_response_size", {}).get("reduce_with") or "",
    ):
        assert not _names_mode(text, current), (current, text)


@pytest.mark.parametrize("mode", _SYNC_MODES)
def test_sync_hints_never_name_the_mode_in_effect(mode):
    formatted = format_response(
        deepcopy(_sync_source(3)), {"response_mode": mode}, task_type="mixed"
    )
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": mode}
    )
    current = formatted.get("_mode") or ("full" if mode == "full" else mode)
    for text in (
        env.get("raw_governance_hint") or "",
        env.get("_response_size", {}).get("reduce_with") or "",
    ):
        assert not _names_mode(text, current), (current, text)


# Direct table over the size hint: every alias that has one, every mode, with
# an envelope padded past the 4,000 B threshold. response_options is absent in
# exactly the cases where the old code could not see the mode in effect.
_PAD = {"padding": "x" * 4_500}


@pytest.mark.parametrize(
    "friendly_name, arguments, payload, current",
    [
        ("check_working_state", {}, {}, "minimal"),
        ("check_working_state", {"include_state": True}, {}, "minimal"),
        ("check_working_state", {"verbosity": "standard"}, {}, "standard"),
        ("check_working_state", {"verbosity": "full"}, {}, "full"),
        ("check_working_state", {"lite": False}, {}, "full"),
        ("sync_state", {"response_mode": "minimal"}, {"_mode": "minimal"}, "minimal"),
        ("sync_state", {}, {"_mode": "compact"}, "compact"),
        ("sync_state", {}, {"_mode": "mirror"}, "mirror"),
        ("sync_state", {"response_mode": "standard"}, {"_mode": "standard"}, "standard"),
        ("sync_state", {"response_mode": "full"}, {}, "full"),
        ("sync_state", {"response_mode": "verbose"}, {}, "full"),
        ("search_shared_memory", {}, {}, "lean"),
        ("search_shared_memory", {"response_mode": "compact"}, {}, "compact"),
        ("search_shared_memory", {"response_mode": "full"}, {}, "full"),
    ],
)
def test_reduce_with_never_names_the_mode_in_effect(
    friendly_name, arguments, payload, current
):
    envelope = {"success": True, "tool": friendly_name, **_PAD}
    _attach_response_size(envelope, friendly_name, arguments, payload)
    reduce_with = envelope["_response_size"].get("reduce_with")
    if reduce_with:
        assert not _names_mode(reduce_with, current), (current, reduce_with)


@pytest.mark.parametrize(
    "arguments",
    [{}, {"response_mode": "minimal"}, {"response_mode": "full"}, {"verbose": True}],
)
def test_start_session_size_receipt_never_suggests_another_mint(arguments):
    """Acting on a start_session size hint would mint a second identity."""
    payload = {
        "success": True,
        "uuid": "54d62846-70bc-41e0-afcf-087d94b5d747",
        "client_session_id": "agent-54d62846-70b",
        "identity_resolution_outcome": "minted_force_new",
        "thread_context": {
            "position": 2,
            "predecessor": {"uuid": "11111111-2222-3333-4444-555555555555"},
            "honest_message": "x" * 4_500,
        },
    }
    env = build_experience_envelope("start_session", "onboard", payload, arguments)
    assert env["response_shape"] == "full"
    assert env["_response_size"]["approx_bytes"] >= 4_000
    assert "reduce_with" not in env["_response_size"]


# ---------------------------------------------------------------------------
# A server-inferred binding is marked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proof_origin, source, marked",
    [
        ("server_inferred", "ip_ua_fingerprint", True),
        ("server_inferred", "sticky_cache:explicit_client_session_id", True),
        ("caller_asserted", "explicit_client_session_id", False),
        (None, None, False),
    ],
)
async def test_inferred_binding_is_marked_on_a_real_read(
    monkeypatch, proof_origin, source, marked
):
    """A nested use_tool dispatch can serve real state on a server-inferred
    binding (the injected agent_id passes the handler's guard). The canonical
    payload cannot say so: agent_signature collapses to {"uuid": null} for a
    server-inferred binding. The description promises identity_assurance
    caller_proven=false, so the envelope reads the request's proof."""
    import src.mcp_handlers.context as context

    monkeypatch.setattr(context, "get_session_proof_origin", lambda: proof_origin)
    monkeypatch.setattr(context, "get_session_resolution_source", lambda: source)
    signature = {"uuid": None} if proof_origin == "server_inferred" else agent_signature()
    payload, validated = await real_metrics_payload({}, signature=signature)
    result = await apply_experience_envelope(
        "get_governance_metrics",
        validated,
        DispatchContext(original_name="check_working_state"),
        [TextContent(type="text", text=json.dumps(payload))],
    )
    env = json.loads(result[0].text)
    assert env["tool"] == "check_working_state"
    if marked:
        assert env["identity_assurance"] == {
            "tier": "weak",
            "caller_proven": False,
            "session_source": source,
        }
    else:
        assert "identity_assurance" not in env


@pytest.mark.asyncio
async def test_unbound_read_is_not_marked(monkeypatch):
    """The unbound payload reads nobody's state; there is nothing to mark."""
    import src.mcp_handlers.context as context
    from src.mcp_handlers.core import unbound_metrics_payload

    monkeypatch.setattr(context, "get_session_proof_origin", lambda: "server_inferred")
    monkeypatch.setattr(context, "get_session_resolution_source", lambda: "ip_ua_fingerprint")
    payload = {"success": True, **unbound_metrics_payload(), "agent_signature": {"uuid": None}}
    result = await apply_experience_envelope(
        "get_governance_metrics",
        {},
        DispatchContext(original_name="check_working_state"),
        [TextContent(type="text", text=json.dumps(payload))],
    )
    assert "identity_assurance" not in json.loads(result[0].text)
