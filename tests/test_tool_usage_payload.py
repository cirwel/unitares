"""#1387 — the action discriminator written into ``audit.tool_usage.payload``.

The question the instrument has to answer is "did an agent request a dialectic
review, and who?". Before this change it could not:

  * ``tool_name`` alone collapses ``dialectic(action='request')`` (a review
    request) into the same bucket as ``dialectic(action='list')`` (a dashboard
    read of the sessions table) — 53,825 rows, 41 of them in the last 30 days,
    all indistinguishable.
  * The MCP-protocol wrapper (``src/tool_registration.py``) — the only path a
    Claude Code / claude.ai client takes — recorded nothing at all.
  * The #425 typed identity refusal is deliberately success-SHAPED, so a
    refused call audited as a succeeding anonymous one.

These tests pin all three, plus the two properties that make the payload safe
to ship: the allowlist is enforced in code (no caller value but the clamped
action token can reach the column), and telemetry failure is never fatal.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.mcp_handlers  # noqa: F401 — registers every tool definition
from src.services import tool_usage_recorder as recorder
from src.services.tool_usage_recorder import (
    build_tool_usage_payload,
    classify_tool_result,
    resolve_audit_agent_id,
)

AGENT_UUID = "7750bf80-20ad-4108-a952-5271b73845b8"


def _consume_coro(coro, name=None):
    if hasattr(coro, "close"):
        coro.close()
    return MagicMock()


def _patch_recorder_io(monkeypatch):
    tracker = MagicMock()
    append = AsyncMock(return_value=True)
    monkeypatch.setattr("src.tool_usage_tracker.get_tool_usage_tracker", lambda: tracker)
    monkeypatch.setattr("src.audit_db.append_tool_usage_async", append)
    monkeypatch.setattr("src.background_tasks.create_tracked_task", _consume_coro)
    return tracker, append


# ---------------------------------------------------------------------------
# The discriminator itself
# ---------------------------------------------------------------------------

def test_dialectic_request_is_distinguishable_from_dialectic_list():
    """The whole point: a review REQUEST must not look like a sessions LIST."""
    request = build_tool_usage_payload("dialectic", {"action": "request"})
    listing = build_tool_usage_payload("dialectic", {"action": "list"})

    assert request["action"] == "request"
    assert listing["action"] == "list"
    assert request != listing


def test_router_default_is_not_mistaken_for_an_explicit_call():
    """``dialectic`` defaults to ``list``; the source separates intent from default.

    Without ``action_source`` a bare ``dialectic()`` and an explicit
    ``dialectic(action='list')`` are byte-identical rows, and the kill gate
    cannot subtract router-defaulted calls.
    """
    defaulted = build_tool_usage_payload("dialectic", {})
    explicit = build_tool_usage_payload("dialectic", {"action": "list"})

    assert defaulted == {"action": "list", "action_source": "default"}
    assert explicit == {"action": "list", "action_source": "explicit"}


def test_alias_records_as_typed_and_carries_the_canonical_name():
    """``request_review`` keeps its own tool_name; canonical is recoverable.

    Splitting a merged canonical row back into alias-vs-not is impossible;
    recovering canonical from the payload is a COALESCE. The information loss
    is asymmetric, so the alias stays in ``tool_name``.
    """
    payload = build_tool_usage_payload("request_review", {"issue_description": "x"})

    assert payload["canonical_tool"] == "dialectic"
    assert payload["action"] == "request"
    assert payload["action_source"] == "alias_injected"


def test_op_synonym_and_case_are_normalized():
    assert build_tool_usage_payload("dialectic", {"op": "THESIS"}) == {
        "action": "thesis",
        "action_source": "explicit",
    }


def test_single_purpose_tools_stay_empty():
    """~97% of rows must not grow a payload — get_governance_metrics alone is
    816k rows/week and a sub-action is not a coherent question for it."""
    assert build_tool_usage_payload("get_governance_metrics", {"agent_id": "x"}) is None
    assert build_tool_usage_payload("onboard", {"spawn_reason": "subagent"}) is None
    # A stray `action` kwarg on a single-purpose tool must not invent one.
    assert build_tool_usage_payload("onboard", {"action": "whatever"}) is None


def test_self_recovery_declares_its_vocabulary():
    """Registered by hand (not an action_router) — without default_action a bare
    call audited as no-action while actually running "check"."""
    assert build_tool_usage_payload("self_recovery", {}) == {
        "action": "check",
        "action_source": "default",
    }
    assert build_tool_usage_payload("self_recovery", {"action": "review"})["action"] == "review"


# ---------------------------------------------------------------------------
# Drift guards — a new action must not become silently unauditable
# ---------------------------------------------------------------------------

def test_every_action_router_declares_its_vocabulary():
    """``action_router`` derives ``known_actions`` from its own routing map, so
    this can only fail if the derivation is removed."""
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    routers = {
        "knowledge", "agent", "calibration", "config", "export",
        "observe", "admin", "dialectic", "research_registry", "self_recovery",
    }
    for name in routers:
        td = _TOOL_DEFINITIONS.get(name)
        assert td is not None, f"{name} is not registered"
        assert td.known_actions, f"{name} declares no known_actions — its rows would carry no discriminator"


def test_every_declared_default_action_is_routable():
    """A ``default_action`` outside ``known_actions`` would audit every
    action-less call as ``action_unlisted``. The decorator refuses it at import
    time; this pins that the live registry is clean."""
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    for name, td in _TOOL_DEFINITIONS.items():
        if td.default_action and td.known_actions:
            assert td.default_action in td.known_actions, name


def test_declaring_an_unroutable_default_action_is_a_hard_error():
    from src.mcp_handlers.decorators import mcp_tool

    with pytest.raises(ValueError, match="not in known_actions"):
        @mcp_tool(
            "a_tool_with_a_bad_default",
            register=False,
            default_action="nope",
            known_actions={"check"},
        )
        async def _handler(arguments):  # pragma: no cover - never called
            return []


def test_every_known_action_is_classified_by_the_stakes_table():
    """The clamp vocabulary and the #775 classification must describe the same
    surface — a known action that only resolves via the fail-closed default
    means one of the two tables drifted.

    External-plugin tools are exempt for the same reason ``test_stakes_table``
    exempts them: they are deliberately unenumerated and fail closed to "high"
    until an operator classifies them. The exemption set is imported rather
    than restated so the two tests cannot disagree about what "external" means.
    Without this the assertion is order-dependent — ``pi`` only appears once
    some other test in the process has imported ``unitares_pi_plugin``.
    """
    from src.mcp_handlers import stakes_table
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS
    from tests.test_stakes_table import _EXTERNAL_PLUGIN_TOOLS

    unclassified = []
    for name, td in _TOOL_DEFINITIONS.items():
        if name in _EXTERNAL_PLUGIN_TOOLS:
            continue
        module = getattr(td.handler, "__module__", "") or ""
        if not module.startswith("src."):
            continue  # external single-purpose tool — fail-closed-high by design
        for action in td.known_actions or ():
            if (
                stakes_table.get_action_stakes(name, action) == "high"
                and (name, action) not in stakes_table._HIGH
                and (name, None) not in stakes_table._HIGH
            ):
                unclassified.append((name, action))
    assert unclassified == [], (
        f"known actions with no stakes classification: {sorted(unclassified)} — "
        f"add them to stakes_table._HIGH or _BASELINE"
    )


# ---------------------------------------------------------------------------
# The allowlist — enforced in code, not by convention
# ---------------------------------------------------------------------------

_FORBIDDEN = {
    "continuity_token": "hmac-ownership-proof-DO-NOT-LEAK",
    "issue_description": "free text describing private work",
    "reasoning": "more free text",
    "root_cause": "more free text",
    "reflection": "more free text",
    "query": "a search query",
    "proposed_conditions": ["a", "list"],
    "thresholds": {"nested": "structure"},
    "file_path": "/Users/someone/private/path",
    "agent_id": AGENT_UUID,
    "client_session_id": "sess-1",
    "target_agent_id": "another-agent",
    "name": "Some Display Name",
}


@pytest.mark.parametrize("tool_name", ["dialectic", "request_review", "knowledge", "onboard"])
def test_no_non_allowlisted_argument_ever_reaches_the_payload(tool_name):
    arguments = {"action": "request", **_FORBIDDEN}
    payload = build_tool_usage_payload(tool_name, arguments) or {}

    assert set(payload) <= {"action", "canonical_tool", "action_source"}
    flat = " ".join(payload.values())
    for value in _FORBIDDEN.values():
        assert str(value) not in flat
    for key in _FORBIDDEN:
        assert key not in payload


def test_unknown_action_is_clamped_not_echoed():
    """A caller-supplied action outside the routing map must never become a
    GROUP BY key — otherwise ``dialectic(action='<4KB of junk>')`` is a
    cardinality bomb in a 2.5M-row table."""
    payload = build_tool_usage_payload("dialectic", {"action": "A" * 5000})
    assert payload["action"] == "action_unlisted"

    payload = build_tool_usage_payload("knowledge", {"action": "definitely_not_an_action"})
    assert payload["action"] == "action_unlisted"


def test_external_plugin_router_falls_through_to_unlisted():
    """A tool this server does not own (unitares_pi_plugin's ``pi``) has no
    known vocabulary — record that a sub-action existed, not what it said."""
    payload = build_tool_usage_payload("a_tool_this_server_does_not_register", {"action": "zap"})
    assert payload["action"] == "action_unlisted"
    assert "zap" not in str(payload)


def test_sanitizer_drops_hand_built_keys_at_the_write_boundary(monkeypatch):
    """Second enforcement layer: even a call site that hands ``record_tool_usage``
    an arbitrary dict cannot land it in the column."""
    _tracker, append = _patch_recorder_io(monkeypatch)

    recorder.record_tool_usage(
        tool_name="dialectic",
        agent_id=AGENT_UUID,
        success=True,
        payload={
            "action": "request",
            "continuity_token": "SECRET",
            "nested": {"a": 1},
            "numeric": 5,
        },
    )

    written = append.call_args.kwargs["payload"]
    assert written == {"action": "request"}


# ---------------------------------------------------------------------------
# Build order: BEFORE dispatch, because the pipeline mutates `arguments`
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payload_is_built_before_alias_injection_mutates_arguments():
    """``params_step.resolve_alias`` writes ``arguments['action'] = 'request'``
    into the CALLER'S dict — ``run_tool_dispatch_pipeline`` makes no copy. A
    payload built after dispatch would call every ``request_review``
    ``action_source='explicit'`` and destroy the alias-adoption signal.
    """
    from src.mcp_handlers.middleware import DispatchContext
    from src.mcp_handlers.middleware.params_step import resolve_alias

    arguments = {"issue_description": "x"}
    before = build_tool_usage_payload("request_review", arguments)

    await resolve_alias("request_review", arguments, DispatchContext())
    assert arguments["action"] == "request"  # the in-place mutation, pinned

    after = build_tool_usage_payload("request_review", arguments)

    assert before["action_source"] == "alias_injected"
    assert after["action_source"] == "explicit"  # what the wrong order records
    assert before != after


# ---------------------------------------------------------------------------
# End-to-end: the payload reaches the DB writer on every surface
# ---------------------------------------------------------------------------

async def _dispatch_that_injects_the_alias_action(_name, arguments):
    """Stand-in for the real pipeline's in-place mutation.

    ``params_step.resolve_alias`` does exactly this to the CALLER'S dict, and
    it runs BEFORE ``validate_params``. Reproducing it here is what makes the
    surface tests below regression tests for the BUILD ORDER: if a call site
    moves its ``build_tool_usage_payload`` after dispatch, ``action_source``
    flips to "explicit" and they fail.

    Do NOT read this as "the pipeline never copies ``arguments``" — it does.
    ``validate_params`` rebinds ``arguments = validated_dict`` for every tool
    with a Pydantic schema, so a HANDLER'S write to ``arguments["agent_id"]``
    never reaches the caller's dict. That asymmetry is the whole reason
    ``resolve_dispatch_bound_agent_id`` exists, and it is pinned end-to-end
    against the REAL pipeline in
    ``test_mcp_wrapper_attributes_request_review_end_to_end``.
    """
    if isinstance(arguments, dict) and "action" not in arguments:
        arguments["action"] = "request"
    return {"success": True}


@pytest.mark.asyncio
async def test_rest_surface_writes_the_payload(monkeypatch):
    from src.services.http_tool_service import execute_http_tool

    _tracker, append = _patch_recorder_io(monkeypatch)
    monkeypatch.setattr(
        "src.services.http_tool_service.get_direct_http_tool_handler", lambda _n: None
    )
    monkeypatch.setattr(
        "src.services.http_tool_service.execute_http_dispatch_fallback",
        _dispatch_that_injects_the_alias_action,
    )

    arguments = {"issue_description": "x"}
    await execute_http_tool("request_review", arguments)

    assert arguments["action"] == "request"  # dispatch really did mutate it
    assert append.call_args.kwargs["payload"] == {
        "canonical_tool": "dialectic",
        "action": "request",
        "action_source": "alias_injected",
    }


@pytest.mark.asyncio
async def test_rest_refusal_path_writes_a_countable_row(monkeypatch):
    """A call that fails auth must be countable, not invisible — and must carry
    the discriminator so "refused review requests" is answerable."""
    from src.services import http_tool_service

    _tracker, append = _patch_recorder_io(monkeypatch)
    monkeypatch.setattr(
        http_tool_service,
        "_strict_identity_refusal_or_none",
        lambda _t, _a: {"status": "identity_required"},
    )

    await http_tool_service.execute_http_tool("request_review", {"issue_description": "x"})

    kwargs = append.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["error_type"] == "identity_required"
    assert kwargs["payload"]["action"] == "request"


@pytest.mark.asyncio
async def test_stdio_surface_writes_the_payload(monkeypatch):
    import src.mcp_server_std as std

    _tracker, append = _patch_recorder_io(monkeypatch)
    monkeypatch.setattr(std, "STDIO_PROXY_HTTP_URL", None, raising=False)
    monkeypatch.setattr(std, "STDIO_PROXY_URL", None, raising=False)
    monkeypatch.setattr(
        "src.mcp_handlers.dispatch_tool", _dispatch_that_injects_the_alias_action
    )

    arguments = {"issue_description": "x"}
    await std.call_tool("request_review", arguments)

    assert arguments["action"] == "request"  # dispatch really did mutate it
    assert append.call_args.kwargs["payload"] == {
        "canonical_tool": "dialectic",
        "action": "request",
        "action_source": "alias_injected",
    }


@pytest.mark.asyncio
async def test_mcp_protocol_wrapper_records_at_all(monkeypatch):
    """The MCP-protocol surface (/mcp, /sse) wrote NOTHING before #1387."""
    import src.tool_registration as tr

    tr._tool_wrappers_cache.clear()
    calls = []
    monkeypatch.setattr(tr, "record_tool_usage", lambda **kw: calls.append(kw))
    monkeypatch.setattr(tr, "_wave3a_get_route", lambda _n: None)
    monkeypatch.setattr(tr, "dispatch_tool", _dispatch_that_injects_the_alias_action)

    try:
        await tr.get_tool_wrapper("request_review")(issue_description="x")
    finally:
        tr._tool_wrappers_cache.clear()

    assert len(calls) == 1
    assert calls[0]["tool_name"] == "request_review"
    assert calls[0]["success"] is True
    assert calls[0]["payload"] == {
        "canonical_tool": "dialectic",
        "action": "request",
        "action_source": "alias_injected",
    }


@pytest.mark.asyncio
async def test_mcp_protocol_wrapper_counts_the_identity_refusal(monkeypatch):
    """The #425 refusal is success-shaped; it must still audit as a failure."""
    import src.tool_registration as tr
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload
    from src.mcp_handlers.response_base import success_response

    tr._tool_wrappers_cache.clear()
    calls = []
    monkeypatch.setattr(tr, "record_tool_usage", lambda **kw: calls.append(kw))
    monkeypatch.setattr(tr, "_wave3a_get_route", lambda _n: None)
    monkeypatch.setattr(
        tr,
        "dispatch_tool",
        AsyncMock(return_value=success_response(strict_identity_refusal_payload("dialectic"))),
    )

    try:
        await tr.get_tool_wrapper("request_review")(issue_description="x")
    finally:
        tr._tool_wrappers_cache.clear()

    assert len(calls) == 1
    assert calls[0]["success"] is False
    assert calls[0]["error_type"] == "identity_required"


# ---------------------------------------------------------------------------
# End-to-end: WHO, through the real dispatch pipeline
# ---------------------------------------------------------------------------

BOUND_UUID = "bb3602ee-9d4c-4a10-9f6e-1a2b3c4d5e6f"


async def _fake_resolve_identity(name, arguments, ctx):
    """A strongly-bound caller, resolved the way the real middleware resolves.

    Mirrors the three ``_attach_middleware_identity`` sites in
    ``resolve_identity`` (identity_step.py:584 / :737 / :1054): it writes the
    handoff keys into the CALLER'S dict and sets a request-scoped session
    context which ``run_tool_dispatch_pipeline`` tears down in its ``finally``.
    Both facts matter — the teardown is why a contextvar fallback cannot
    rescue attribution on this surface.
    """
    from src.mcp_handlers.context import set_session_context
    from src.mcp_handlers.middleware import identity_step

    identity_step._attach_middleware_identity(
        arguments,
        session_key="sk-e2e",
        identity_result={"agent_uuid": BOUND_UUID, "source": "test"},
    )
    ctx.context_token = set_session_context(
        session_key="sk-e2e", client_session_id="csid-e2e", agent_id=BOUND_UUID
    )
    ctx.session_key = "sk-e2e"
    ctx.bound_agent_id = BOUND_UUID
    return name, arguments, ctx


async def _stub_handler(_arguments):
    from mcp.types import TextContent

    import json as _json

    return [TextContent(type="text", text=_json.dumps({"success": True}))]


@pytest.mark.parametrize(
    "invoked, kwargs, canonical",
    [
        # The #1387 question itself. `dialectic` is in inject_identity's
        # `browsable_data_tools`, so `arguments["agent_id"]` is NEVER written
        # for it, and the handler's own write lands in validate_params' copy.
        ("request_review", {"issue_description": "e2e"}, "dialectic"),
        ("dialectic", {"action": "request", "issue_description": "e2e"}, "dialectic"),
        # Same carve-out via `knowledge_browsable_actions`.
        ("knowledge", {"action": "search", "query": "e2e"}, "knowledge"),
        # Control: a non-browsable tool, where inject_identity's in-place
        # write already survived. Must keep working.
        ("sync_state", {"summary": "e2e"}, "process_agent_update"),
    ],
)
@pytest.mark.asyncio
async def test_mcp_wrapper_attributes_request_review_end_to_end(
    monkeypatch, invoked, kwargs, canonical
):
    """The row must say WHO, not just THAT — through the real pipeline.

    Before this, every one of the browsable-carve-out tools wrote
    ``agent_id=NULL`` from the MCP-protocol surface even for a strongly bound
    caller: countable, unattributed, and the PR's own title claim unmet. Only
    ``resolve_identity`` and the handler are stubbed; ``resolve_alias``,
    ``inject_identity``, ``validate_params`` and
    ``run_tool_dispatch_pipeline`` (including its context ``finally``) are the
    real ones.
    """
    import src.mcp_handlers as mh
    import src.mcp_handlers.middleware as mw
    import src.tool_registration as tr

    _tracker, append = _patch_recorder_io(monkeypatch)
    monkeypatch.setattr(tr, "_wave3a_get_route", lambda _n: None)
    monkeypatch.setitem(
        mw.PRE_DISPATCH_STEPS,
        mw.PRE_DISPATCH_STEPS.index(mw.resolve_identity),
        _fake_resolve_identity,
    )
    monkeypatch.setitem(mh.TOOL_HANDLERS, canonical, _stub_handler)

    tr._tool_wrappers_cache.clear()
    try:
        await tr.get_tool_wrapper(invoked)(**kwargs)
    finally:
        tr._tool_wrappers_cache.clear()

    assert append.call_count == 1
    assert append.call_args.kwargs["agent_id"] == BOUND_UUID


def test_dispatch_bound_attribution_ignores_a_caller_supplied_handoff_key():
    """``_middleware_identity_result`` is server-written — ``resolve_identity``
    opens with an unconditional ``_clear_middleware_identity``. The reader is
    still UUID-clamped so a malformed value can never enter the column."""
    from src.services.tool_usage_recorder import resolve_dispatch_bound_agent_id

    assert resolve_dispatch_bound_agent_id({}) is None
    assert resolve_dispatch_bound_agent_id(None) is None
    assert (
        resolve_dispatch_bound_agent_id({"_middleware_identity_result": "not-a-dict"})
        is None
    )
    assert (
        resolve_dispatch_bound_agent_id(
            {"_middleware_identity_result": {"agent_uuid": "Claude_Code_20260727"}}
        )
        is None
    )
    assert (
        resolve_dispatch_bound_agent_id(
            {"_middleware_identity_result": {"agent_uuid": BOUND_UUID}}
        )
        == BOUND_UUID
    )


# ---------------------------------------------------------------------------
# Refusal classification + attribution
# ---------------------------------------------------------------------------

def test_typed_identity_refusal_classifies_as_failure():
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload

    payload = {"success": True, **strict_identity_refusal_payload("dialectic")}
    assert classify_tool_result(payload) == (False, "identity_required")

    lineage = {
        "success": True,
        **strict_identity_refusal_payload("onboard", status="lineage_declaration_required"),
    }
    assert classify_tool_result(lineage) == (False, "lineage_declaration_required")


def test_governance_verdicts_are_still_successes():
    """A pause/reject verdict is a SUCCESSFUL call — the refusal detector must
    not widen into governance outcomes (that circularity is why
    ``state_error`` is excluded)."""
    assert classify_tool_result({"success": True, "verdict": "pause"}) == (True, None)
    assert classify_tool_result(
        {"success": False, "error_category": "state_error"}
    ) == (True, None)


def test_attribution_falls_back_to_the_resolved_binding():
    """``identity`` (3,049 rows/30d) and every ``pre_onboard`` read audited as
    anonymous because the recorder only ever read ``arguments['agent_id']``.

    Driven through the REAL context primitives, not a patched getter: what
    makes a value attributable is that ``update_context_agent_id`` — the sole
    write path identity resolution takes — put it there.
    """
    from src.mcp_handlers.context import (
        reset_session_context,
        set_session_context,
        update_context_agent_id,
    )

    token = set_session_context(session_key="sk", client_session_id="csid")
    try:
        update_context_agent_id(AGENT_UUID)  # what a resolver does
        assert resolve_audit_agent_id(None) == AGENT_UUID
        # Request-side always wins.
        assert resolve_audit_agent_id("explicit-value") == "explicit-value"
    finally:
        reset_session_context(token)


def test_unverified_x_agent_id_header_is_never_attribution():
    """SECURITY: ``http_api`` seeds the session context with the raw
    ``X-Agent-Id`` header BEFORE any resolution runs, and for the two tools
    #1387 targets nothing overwrites it — ``identity`` is in
    ``_resolve_http_bound_agent``'s ``skip_tools`` and every ``pre_onboard``
    read returns at the #945 guard. A UUID clamp does not save this: it only
    makes the forged value JOINABLE, which is worse than NULL. Attribution
    must require that a resolver actually wrote the binding.
    """
    from src.mcp_handlers.context import reset_session_context, set_session_context

    forged = "11111111-2222-3333-4444-555555555555"
    # Exactly what src/http_api.py does at the REST entry point:
    #   set_session_context(..., agent_id=x_agent_id or arguments.get("agent_id"))
    token = set_session_context(
        session_key=None, client_session_id=None, agent_id=forged
    )
    try:
        assert resolve_audit_agent_id(None) is None
    finally:
        reset_session_context(token)


def test_attribution_fallback_admits_only_uuids():
    """Second belt: even a RESOLVED non-UUID label (``audit.tool_usage.agent_id``
    carries a mix of UUIDs and structured labels) may not enter via the
    fallback — it could only ever add an unjoinable string."""
    from src.mcp_handlers.context import (
        reset_session_context,
        set_session_context,
        update_context_agent_id,
    )

    token = set_session_context(session_key="sk")
    try:
        update_context_agent_id("Claude_Code_20260727")
        assert resolve_audit_agent_id(None) is None
    finally:
        reset_session_context(token)


def test_presence_refresh_still_keys_on_the_request_side_id(monkeypatch):
    """The agent:/ presence lease is a liveness claim on another surface —
    the audit-only attribution fallback must not widen who refreshes it."""
    from src.mcp_handlers.context import (
        reset_session_context,
        set_session_context,
        update_context_agent_id,
    )

    _patch_recorder_io(monkeypatch)
    scheduled = []
    monkeypatch.setattr(
        "src.mcp_handlers.identity.agent_presence_lease.schedule_agent_presence_heartbeat",
        lambda agent_id, client_session_id=None: scheduled.append(agent_id),
    )

    token = set_session_context(session_key="sk")
    try:
        update_context_agent_id(AGENT_UUID)
        recorder.record_tool_usage(tool_name="knowledge", agent_id=None, success=True)
    finally:
        reset_session_context(token)

    assert scheduled == []


def test_jsonl_sink_keeps_the_request_side_agent_id(monkeypatch):
    """The JSONL tracker is a behavioural-sensor INPUT, not a log.

    ``get_usage_stats(agent_id=..., window_hours=1)`` feeds
    ``compute_behavioral_sensor_eisv`` (``E = 0.85E + 0.15*(1-err)``,
    ``I = 0.90I + 0.10*ratio``) from five live call sites, and a per-agent
    query only fires at all when ``tu_total > 0``. Newly-attributed rows
    would flip that guard from never-executing to executing — turning a
    verdict-relevant signal ON as a telemetry side effect. The audit
    attribution fallback is confined to ``audit.tool_usage``.
    """
    from src.mcp_handlers.context import (
        reset_session_context,
        set_session_context,
        update_context_agent_id,
    )

    tracker, append = _patch_recorder_io(monkeypatch)

    token = set_session_context(session_key="sk")
    try:
        update_context_agent_id(AGENT_UUID)
        recorder.record_tool_usage(tool_name="identity", agent_id=None, success=True)
    finally:
        reset_session_context(token)

    assert tracker.log_tool_call.call_args.kwargs["agent_id"] is None
    assert append.call_args.kwargs["agent_id"] == AGENT_UUID


def test_audit_only_writes_the_audit_row_and_nothing_else(monkeypatch):
    """``audit_only`` is what keeps a NEWLY instrumented surface from also
    enrolling itself in the JSONL sensor feed and the agent:/ presence lease."""
    tracker, append = _patch_recorder_io(monkeypatch)
    scheduled = []
    monkeypatch.setattr(
        "src.mcp_handlers.identity.agent_presence_lease.schedule_agent_presence_heartbeat",
        lambda agent_id, client_session_id=None: scheduled.append(agent_id),
    )

    recorder.record_tool_usage(
        tool_name="knowledge", agent_id=AGENT_UUID, success=True, audit_only=True
    )

    assert append.call_args.kwargs["agent_id"] == AGENT_UUID
    assert tracker.log_tool_call.call_count == 0
    assert scheduled == []

    # Default is unchanged for every pre-existing caller (REST + stdio).
    recorder.record_tool_usage(tool_name="knowledge", agent_id=AGENT_UUID, success=True)
    assert tracker.log_tool_call.call_count == 1
    assert scheduled == [AGENT_UUID]


@pytest.mark.asyncio
async def test_mcp_protocol_wrapper_is_audit_only(monkeypatch):
    """Regression for the transport widening: ``src/tool_registration.py`` is
    the FastMCP /mcp + /sse wrapper and wrote NOTHING before #1387. Adding an
    audit row must not make a whole transport start refreshing agent:/ leases
    — that is a presence change, and this PR measured no presence question.
    ``knowledge`` is in ``_PRESENCE_REFRESH_TOOLS``, so this would fire.
    """
    import src.tool_registration as tr

    tracker, append = _patch_recorder_io(monkeypatch)
    scheduled = []
    monkeypatch.setattr(
        "src.mcp_handlers.identity.agent_presence_lease.schedule_agent_presence_heartbeat",
        lambda agent_id, client_session_id=None: scheduled.append(agent_id),
    )
    monkeypatch.setattr(tr, "_wave3a_get_route", lambda _n: None)
    monkeypatch.setattr(tr, "dispatch_tool", _dispatch_that_injects_the_alias_action)

    tr._tool_wrappers_cache.clear()
    try:
        await tr.get_tool_wrapper("knowledge")(
            action="search", query="x", agent_id=AGENT_UUID
        )
    finally:
        tr._tool_wrappers_cache.clear()

    assert append.call_count == 1
    assert append.call_args.kwargs["agent_id"] == AGENT_UUID
    assert tracker.log_tool_call.call_count == 0
    assert scheduled == []


# ---------------------------------------------------------------------------
# Telemetry failure is never fatal
# ---------------------------------------------------------------------------

def test_payload_build_failure_is_non_fatal():
    with patch(
        "src.mcp_handlers.decorators.resolve_canonical_action_and_source",
        side_effect=RuntimeError("resolver exploded"),
    ):
        assert build_tool_usage_payload("dialectic", {"action": "request"}) is None


def test_recorder_survives_every_sink_failing(monkeypatch):
    monkeypatch.setattr(
        "src.tool_usage_tracker.get_tool_usage_tracker",
        MagicMock(side_effect=RuntimeError("jsonl down")),
    )
    # Plain MagicMock (not AsyncMock): returns a sentinel rather than a live
    # coroutine, so the raising create_tracked_task below cannot orphan one.
    monkeypatch.setattr("src.audit_db.append_tool_usage_async", MagicMock())
    monkeypatch.setattr(
        "src.background_tasks.create_tracked_task",
        MagicMock(side_effect=RuntimeError("db down")),
    )

    recorder.record_tool_usage(
        tool_name="dialectic",
        agent_id=AGENT_UUID,
        success=True,
        payload={"action": "request"},
    )


@pytest.mark.asyncio
async def test_wrapper_survives_a_broken_recorder(monkeypatch):
    """A telemetry explosion must never break, or change, a tool call."""
    import json as _json

    from mcp.types import TextContent

    import src.tool_registration as tr

    handler_response = [
        TextContent(type="text", text=_json.dumps({"success": True, "sessions": []}))
    ]

    tr._tool_wrappers_cache.clear()
    monkeypatch.setattr(
        tr, "record_tool_usage", MagicMock(side_effect=RuntimeError("telemetry down"))
    )
    monkeypatch.setattr(tr, "_wave3a_get_route", lambda _n: None)
    monkeypatch.setattr(tr, "dispatch_tool", AsyncMock(return_value=handler_response))

    try:
        result = await tr.get_tool_wrapper("dialectic")(action="list")
    finally:
        tr._tool_wrappers_cache.clear()

    # The caller must still get the HANDLER'S response, not the wrapper's
    # error envelope — telemetry may not change what a tool returns.
    assert result == {"success": True, "sessions": []}
