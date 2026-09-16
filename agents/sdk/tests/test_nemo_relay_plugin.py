"""Tests for ``unitares_sdk.integrations.nemo_relay``.

The unit tests drive the exporter's state machine with event dictionaries in
the exact shape ``nemo_relay`` emits (captured from Relay 0.8.4) and never
import the runtime, so they run in every CI lane. The integration tests
import the runtime and skip when the optional extra is absent.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid as uuid_mod
from typing import Any

import pytest

from unitares_sdk.integrations.nemo_relay import (
    BLOCKING_VERDICTS,
    GATE_MESSAGE_PREFIX,
    GATE_NAME,
    SUBSTRATE_EPISTEMIC_CLASS,
    RelayExportWorker,
    RelayGovernanceExporter,
    RelayPluginConfig,
    UnitaresRelayPlugin,
    substrate_summary,
)
from unitares_sdk.models import CheckinResult, OnboardResult


# --------------------------------------------------------------------------
# Fakes and event builders
# --------------------------------------------------------------------------


class FakeClient:
    """Stands in for ``SyncGovernanceClient``: records calls, scripts verdicts."""

    _ids = itertools.count(1)

    def __init__(self, log: list[tuple[str, Any, dict]], verdicts: list[str] | None = None):
        self.log = log
        self.verdicts = list(verdicts or [])
        self.n = next(self._ids)
        self.closed = False

    def onboard(self, name: str, **kwargs: Any) -> OnboardResult:
        self.log.append(("onboard", name, kwargs))
        return OnboardResult(success=True, client_session_id=f"csid-{self.n}", uuid=f"uuid-{self.n}", verdict="proceed")

    def checkin(self, response_text: str, **kwargs: Any) -> CheckinResult:
        self.log.append(("checkin", response_text, kwargs))
        verdict = self.verdicts.pop(0) if self.verdicts else "proceed"
        guidance = "slow down and verify" if verdict in BLOCKING_VERDICTS else None
        return CheckinResult(success=True, verdict=verdict, guidance=guidance)

    def call_tool(self, tool: str, args: dict) -> dict:
        self.log.append((tool, None, args))
        return {"success": True}

    def close(self) -> None:
        self.closed = True


class RaisingClient(FakeClient):
    def onboard(self, name: str, **kwargs: Any) -> OnboardResult:
        raise RuntimeError("server unreachable")


def factory_for(log: list, verdicts: list[str] | None = None):
    return lambda: FakeClient(log, verdicts)


def scope_start(uuid: str, parent: str, name: str, category: str, data: Any = None) -> dict:
    return {
        "kind": "scope",
        "scope_category": "start",
        "name": name,
        "category": category,
        "uuid": uuid,
        "parent_uuid": parent,
        "attributes": [],
        "data": data,
        "metadata": None,
        "category_profile": None,
    }


def scope_end(uuid: str, parent: str, name: str, category: str, *, status: str = "OK", data: Any = None, error: str | None = None) -> dict:
    metadata: dict[str, Any] = {"otel.status_code": status}
    if error:
        metadata.update({"error.type": "internal_error", "exception.type": error, "otel.status_description": f"internal error: {error}: boom"})
    return {
        "kind": "scope",
        "scope_category": "end",
        "name": name,
        "category": category,
        "uuid": uuid,
        "parent_uuid": parent,
        "attributes": [],
        "data": data,
        "metadata": metadata,
        "category_profile": None,
    }


def tool_pair(parent: str, name: str, *, status: str = "OK", error: str | None = None) -> list[dict]:
    tid = str(uuid_mod.uuid4())
    return [scope_start(tid, parent, name, "tool", {"q": "x"}), scope_end(tid, parent, name, "tool", status=status, error=error)]


ROOT_PARENT = "stack-root-never-seen"


def start_run(exporter: RelayGovernanceExporter, run_id: str = "run-1", turn_id: str = "turn-1") -> None:
    exporter.ingest(scope_start(run_id, ROOT_PARENT, "run-1", "agent"))
    exporter.ingest(scope_start(turn_id, run_id, "turn-1", "agent"))


def calls(log: list, kind: str) -> list[tuple[str, Any, dict]]:
    return [entry for entry in log if entry[0] == kind]


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_config_diagnostics_flag_unknown_keys_and_bad_types():
    diags = RelayPluginConfig.diagnostics({"rest_url": 5, "nope": 1, "checkin_every_tool_calls": 0, "enforce": "yes"})
    by_field = {d["field"]: d for d in diags}
    assert by_field["rest_url"]["level"] == "error"
    assert by_field["nope"]["level"] == "warning"
    assert by_field["checkin_every_tool_calls"]["level"] == "error"
    assert by_field["enforce"]["level"] == "error"
    with pytest.raises(ValueError):
        RelayPluginConfig.from_mapping({"rest_url": 5})


def test_config_from_mapping_ignores_unknown_keys_and_keeps_defaults():
    cfg = RelayPluginConfig.from_mapping({"checkin_every_tool_calls": 3, "nope": 1})
    assert cfg.checkin_every_tool_calls == 3
    assert cfg.enforce is True and cfg.fail_closed is False
    assert RelayPluginConfig.diagnostics(None) == []


# --------------------------------------------------------------------------
# Exporter state machine
# --------------------------------------------------------------------------


def test_root_scope_mints_a_fresh_identity_and_children_map_to_it():
    log: list = []
    exporter = RelayGovernanceExporter(factory_for(log))
    start_run(exporter)
    (_, name, kwargs), = calls(log, "onboard")
    assert name == "relay-run:run-1"
    assert kwargs["force_new"] is True
    assert kwargs["client_hint"] == "nemo-relay"
    assert "parent_agent_id" not in kwargs
    run = exporter.run_for_scope("turn-1")
    assert run is not None and run.bound and run.agent_uuid == "uuid-1"
    assert exporter.run_for_scope("run-1") is run


def test_nested_agent_scope_stays_in_the_parent_run_unless_configured():
    log: list = []
    exporter = RelayGovernanceExporter(factory_for(log))
    start_run(exporter)
    exporter.ingest(scope_start("sub-1", "turn-1", "worker", "agent"))
    assert exporter.run_for_scope("sub-1") is exporter.run_for_scope("run-1")
    assert len(calls(log, "onboard")) == 1

    log2: list = []
    exporter2 = RelayGovernanceExporter(factory_for(log2), RelayPluginConfig(subagent_identities=True))
    start_run(exporter2)
    exporter2.ingest(scope_start("sub-1", "turn-1", "worker", "agent"))
    # Every nested agent scope becomes a lineage-linked identity: the turn
    # under the run, and the worker under the turn.
    onboards = calls(log2, "onboard")
    assert [name for _, name, _ in onboards] == ["relay-run:run-1", "relay-run:turn-1", "relay-run:worker"]
    assert "parent_agent_id" not in onboards[0][2]
    assert onboards[1][2]["parent_agent_id"] == exporter2.run_for_scope("run-1").agent_uuid
    assert onboards[2][2]["spawn_reason"] == "subagent"
    assert onboards[2][2]["parent_agent_id"] == exporter2.run_for_scope("turn-1").agent_uuid
    assert exporter2.run_for_scope("sub-1") is not exporter2.run_for_scope("run-1")


def test_cadence_checkin_is_a_substrate_reading_without_confidence():
    log: list = []
    exporter = RelayGovernanceExporter(factory_for(log), RelayPluginConfig(checkin_every_tool_calls=2))
    start_run(exporter)
    for ev in tool_pair("turn-1", "search"):
        exporter.ingest(ev)
    assert calls(log, "checkin") == []
    for ev in tool_pair("turn-1", "read_file"):
        exporter.ingest(ev)
    (_, text, kwargs), = calls(log, "checkin")
    assert kwargs["epistemic_class"] == SUBSTRATE_EPISTEMIC_CLASS
    assert "confidence" not in kwargs
    assert 0.0 <= kwargs["complexity"] <= 1.0
    assert "2 tool calls" in text and "search, read_file" in text
    assert exporter.run_for_scope("run-1").since_checkin == 0


def test_tool_error_records_a_task_failed_outcome_with_honest_provenance():
    log: list = []
    exporter = RelayGovernanceExporter(factory_for(log))
    start_run(exporter)
    for ev in tool_pair("turn-1", "flaky", status="ERROR", error="ValueError"):
        exporter.ingest(ev)
    (_, _, args), = calls(log, "outcome_event")
    assert args["outcome_type"] == "task_failed"
    assert args["is_bad"] is True
    assert args["detail"]["tool"] == "flaky"
    assert args["detail"]["exception_type"] == "ValueError"
    assert "verification_source" not in args  # in-process evidence keeps the server default
    assert exporter.run_for_scope("run-1").tool_failures == 1


def test_pause_verdict_blocks_tools_and_the_message_names_the_review_path():
    log: list = []
    exporter = RelayGovernanceExporter(factory_for(log, verdicts=["pause"]), RelayPluginConfig(checkin_every_tool_calls=1))
    start_run(exporter)
    assert exporter.gate_for_scope("turn-1", "search") is None
    for ev in tool_pair("turn-1", "search"):
        exporter.ingest(ev)
    message = exporter.gate_for_scope("turn-1", "write_file")
    assert message is not None
    assert "policy 'pause'" in message and "write_file" in message
    assert "slow down and verify" in message
    assert "request_review" in message
    # A scope the worker has not mapped yet resolves through its parent.
    assert exporter.gate_for_scope("guardrail-scope-unknown", "write_file", parent_uuid="turn-1") == message


def test_unknown_scope_is_fail_open_by_default_and_fail_closed_on_request():
    log: list = []
    assert RelayGovernanceExporter(factory_for(log)).gate_for_scope("nope", "t") is None
    closed = RelayGovernanceExporter(factory_for(log), RelayPluginConfig(fail_closed=True))
    message = closed.gate_for_scope("nope", "t")
    assert message is not None and "fail_closed" in message


def test_own_gate_rejections_are_counted_but_never_fed_back_as_outcomes():
    log: list = []
    exporter = RelayGovernanceExporter(factory_for(log))
    start_run(exporter)
    exporter.ingest(scope_start("g-1", "turn-1", GATE_NAME, "guardrail", {"kind": "tool_conditional_execution", "target_name": "rm"}))
    exporter.ingest(scope_end("g-1", "turn-1", GATE_NAME, "guardrail", data={"allowed": False, "rejected": True, "rejection_reason": "paused"}))
    exporter.ingest(scope_start("g-3", "turn-1", "plugin:unitares-x:" + GATE_NAME, "guardrail", {"kind": "tool_conditional_execution", "target_name": "rm"}))
    exporter.ingest(scope_end("g-3", "turn-1", "plugin:unitares-x:" + GATE_NAME, "guardrail", data={"allowed": False, "rejected": True, "rejection_reason": "paused"}))
    exporter.ingest(scope_start("g-4", "turn-1", "opaque-name", "guardrail", {"kind": "tool_conditional_execution", "target_name": "rm"}))
    exporter.ingest(scope_end("g-4", "turn-1", "opaque-name", "guardrail", data={"allowed": False, "rejected": True, "rejection_reason": GATE_MESSAGE_PREFIX + " 'pause' is in force"}))
    exporter.ingest(scope_start("g-2", "turn-1", "pii-shield", "guardrail", {"kind": "tool_conditional_execution", "target_name": "send"}))
    exporter.ingest(scope_end("g-2", "turn-1", "pii-shield", "guardrail", data={"allowed": False, "rejected": True, "rejection_reason": "pii"}))
    outcomes = calls(log, "outcome_event")
    assert len(outcomes) == 1
    assert outcomes[0][2]["outcome_type"] == "tool_rejected"
    assert outcomes[0][2]["detail"]["guardrail"] == "pii-shield"
    assert exporter.run_for_scope("run-1").guardrail_rejections == 4


def test_run_end_files_a_final_checkin_and_clears_state():
    log: list = []
    exporter = RelayGovernanceExporter(factory_for(log))
    start_run(exporter)
    for ev in tool_pair("turn-1", "search"):
        exporter.ingest(ev)
    exporter.ingest(scope_end("turn-1", "run-1", "turn-1", "agent"))
    exporter.ingest(scope_end("run-1", ROOT_PARENT, "run-1", "agent", status="ERROR"))
    (_, text, _), = calls(log, "checkin")
    assert "run end" in text and "run status ERROR" in text
    (_, _, args), = calls(log, "outcome_event")
    assert args["detail"] == {"kind": "run_error", "run": "run-1"}
    assert exporter.run_for_scope("run-1") is None and exporter.run_for_scope("turn-1") is None
    # A later event for the dead run is ignored, not resurrected: a tool scope
    # with an unknown parent never mints an identity.
    for ev in tool_pair("turn-1", "late"):
        exporter.ingest(ev)
    assert len(calls(log, "checkin")) == 1
    assert len(calls(log, "onboard")) == 1


def test_client_failures_never_propagate_and_leave_the_run_unbound():
    log: list = []
    exporter = RelayGovernanceExporter(lambda: RaisingClient(log))
    start_run(exporter)
    run = exporter.run_for_scope("turn-1")
    assert run is not None and not run.bound
    for ev in tool_pair("turn-1", "flaky", status="ERROR", error="OSError"):
        exporter.ingest(ev)
    assert calls(log, "outcome_event") == []
    assert exporter.gate_for_scope("turn-1", "t") is None
    exporter.ingest({"kind": "scope", "scope_category": "end"})  # malformed: no uuid
    exporter.ingest({"kind": "mark", "name": "x"})


def test_substrate_summary_reports_shape_only():
    log: list = []
    exporter = RelayGovernanceExporter(factory_for(log))
    start_run(exporter)
    run = exporter.run_for_scope("run-1")
    text = substrate_summary(run, reason="cadence")
    assert text.startswith("relay substrate reading (cadence) for run 'run-1'")
    assert "good" not in text and "bad" not in text


# --------------------------------------------------------------------------
# Worker and plugin object (no Relay import)
# --------------------------------------------------------------------------


def test_worker_flush_waits_for_queued_events_and_close_is_idempotent():
    seen: list = []
    worker = RelayExportWorker(seen.append)
    worker.start()
    for i in range(50):
        worker.submit({"i": i})
    assert worker.flush(timeout=5.0)
    assert [p["i"] for p in seen] == list(range(50))
    worker.close()
    worker.close()
    worker.submit({"dropped": True})
    assert len(seen) == 50


class FakeContext:
    def __init__(self) -> None:
        self.subscribers: dict[str, Any] = {}
        self.guardrails: dict[str, tuple[int, Any]] = {}

    def register_subscriber(self, name: str, callback: Any) -> None:
        self.subscribers[name] = callback

    def register_tool_conditional_execution_guardrail(self, name: str, priority: int, callback: Any) -> None:
        self.guardrails[name] = (priority, callback)


class FakeEvent:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return self._payload


def test_plugin_registers_subscriber_and_gate_and_streams_events_to_the_exporter():
    log: list = []
    plugin = UnitaresRelayPlugin(factory_for(log, verdicts=["reject"]))
    assert plugin.validate({"checkin_every_tool_calls": 1}) is None
    assert plugin.validate({"bogus": 1})[0]["level"] == "warning"
    ctx = FakeContext()
    plugin.register({"checkin_every_tool_calls": 1, "guardrail_priority": 7}, ctx)
    try:
        assert set(ctx.subscribers) == {"unitares.exporter"}
        assert ctx.guardrails[GATE_NAME][0] == 7
        emit = ctx.subscribers["unitares.exporter"]
        emit(FakeEvent(scope_start("run-1", ROOT_PARENT, "run-1", "agent")))
        emit(FakeEvent(scope_start("turn-1", "run-1", "turn-1", "agent")))
        for ev in tool_pair("turn-1", "search"):
            emit(FakeEvent(ev))
        assert plugin.flush(timeout=5.0)
        assert plugin.exporter.run_for_scope("turn-1").verdict == "reject"
        # The gate itself needs Relay's current scope; outside Relay it fails open.
        assert ctx.guardrails[GATE_NAME][1]("search", {}) is None
    finally:
        plugin.close()


def test_plugin_without_enforce_registers_no_gate():
    plugin = UnitaresRelayPlugin(factory_for([]))
    ctx = FakeContext()
    plugin.register({"enforce": False}, ctx)
    try:
        assert ctx.guardrails == {}
    finally:
        plugin.close()


# --------------------------------------------------------------------------
# Integration against the real runtime (skipped without the extra)
# --------------------------------------------------------------------------


def test_relay_runtime_end_to_end_exports_and_enforces():
    nemo_relay = pytest.importorskip("nemo_relay")
    from nemo_relay import ScopeType, ToolExecutionResult, scope, subscribers, tools
    from nemo_relay import plugin as relay_plugin

    from unitares_sdk.integrations.nemo_relay import install

    log: list = []
    kind = f"unitares-test-{uuid_mod.uuid4().hex[:8]}"
    plugin = install(factory_for(log, verdicts=["pause"]), kind=kind)

    async def ok_tool(args: Any) -> ToolExecutionResult:
        return ToolExecutionResult({"ok": True})

    async def bad_tool(args: Any) -> ToolExecutionResult:
        raise ValueError("boom")

    async def main() -> None:
        config = relay_plugin.PluginConfig(
            components=[relay_plugin.ComponentSpec(kind=kind, config={"checkin_every_tool_calls": 1})]
        )
        await relay_plugin.initialize(config)
        try:
            with scope.scope("e2e-run", ScopeType.Agent):
                with scope.scope("turn", ScopeType.Agent):
                    result = await tools.execute("search", {"q": "x"}, ok_tool)
                    assert result.result == {"ok": True}
                    await subscribers.flush_async()
                    assert plugin.flush(timeout=5.0)
                    # The first tool call filed a check-in whose verdict was pause.
                    with pytest.raises(RuntimeError) as excinfo:
                        await tools.execute("write_file", {"path": "x"}, ok_tool)
                    assert "guardrail rejected" in str(excinfo.value)
                    assert "policy 'pause'" in str(excinfo.value)
                    assert "request_review" in str(excinfo.value)
            await subscribers.flush_async()
            assert plugin.flush(timeout=5.0)
        finally:
            await relay_plugin.clear_async()

    try:
        asyncio.run(main())
    finally:
        plugin.close()
        relay_plugin.deregister(kind)

    (_, name, kwargs), = calls(log, "onboard")
    assert name == "relay-run:e2e-run" and kwargs["force_new"] is True
    checkins = calls(log, "checkin")
    assert len(checkins) >= 2  # cadence check-in, then the run-end check-in
    assert all(k["epistemic_class"] == SUBSTRATE_EPISTEMIC_CLASS for _, _, k in checkins)
    assert "run end" in checkins[-1][1]
    # Our own gate's rejection was counted but not filed as an outcome.
    assert calls(log, "outcome_event") == []
