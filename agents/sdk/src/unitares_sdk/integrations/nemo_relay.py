"""UNITARES on NeMo Relay: an exporter and a policy gate.

NeMo Relay (``nemo-relay`` on PyPI, Apache-2.0) is NVIDIA's multi-language
agent runtime. It owns the transport this SDK used to need host hooks for:
every run, tool call and LLM call becomes a lifecycle event, and middleware at
the tool boundary can refuse a call before it executes. This module attaches
UNITARES to that runtime as a Relay *plugin* and does two things:

1. **Exporter.** Each Relay run (a root scope) becomes one UNITARES process
   identity, minted fresh per the strict-identity contract. Tool and LLM
   activity is folded into substrate check-ins whose epistemic class is
   ``substrate_interpretation``: a reading of the run's shape by the
   integration, never a report authored by the agent. No confidence is ever
   sent, because the integration holds no belief about the work. Tool errors
   become ``task_failed`` outcome events; guardrail rejections become
   ``tool_rejected`` outcome events, except rejections issued by this
   module's own gate, which are never fed back as outcomes so the governor
   does not grade its own enforcement.
2. **Policy gate.** A Relay tool conditional-execution guardrail refuses tool
   calls for a run whose latest UNITARES policy action is ``pause`` or
   ``reject``. The rejection message carries the verdict, the guidance, and
   the review path, so a refusal is contestable rather than opaque.

Provenance is stated honestly: outcome events keep the server's default
``verification_source`` (``agent_reported_tool_result``). The integration runs
inside the agent's process, so what it records is in-band evidence, not an
independent signal, and it must not be labelled ``external_signal``.

Threading. Relay dispatches subscribers on its own thread and requires them to
be infallible and quick, so the subscriber only enqueues the event's dictionary.
A private worker thread drains the queue and talks to the server through the
synchronous REST client. The gate never touches the network: it reads policy
state the worker keeps under a lock, so enforcement latency is bounded by the
check-in cadence, not by a request.

Usage::

    from nemo_relay import plugin as relay_plugin
    from unitares_sdk.integrations.nemo_relay import install

    unitares = install()                 # registers plugin kind "unitares"
    await relay_plugin.initialize(relay_plugin.PluginConfig(components=[
        relay_plugin.ComponentSpec(kind="unitares", config={
            "rest_url": "http://127.0.0.1:8767/v1/tools/call",
            "checkin_every_tool_calls": 20,
        }),
    ]))

The ``nemo_relay`` package is imported lazily, only by ``install()`` and by the
gate, so importing this module never requires the extra.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "BLOCKING_VERDICTS",
    "DEFAULT_REST_URL",
    "PLUGIN_KIND",
    "SUBSTRATE_EPISTEMIC_CLASS",
    "RelayExportWorker",
    "RelayGovernanceExporter",
    "RelayPluginConfig",
    "RunRecord",
    "UnitaresRelayPlugin",
    "install",
    "substrate_summary",
]

PLUGIN_KIND = "unitares"
DEFAULT_REST_URL = "http://127.0.0.1:8767/v1/tools/call"
SUBSTRATE_EPISTEMIC_CLASS = "substrate_interpretation"
BLOCKING_VERDICTS = frozenset({"pause", "reject"})
GATE_NAME = "unitares.policy"
SUBSCRIBER_NAME = "unitares.exporter"
GATE_MESSAGE_PREFIX = "UNITARES policy"
# Scope categories that may stand for a run of their own. A tool, LLM, or
# guardrail scope with no known parent is noise, never an identity.
RUN_ROOT_CATEGORIES = frozenset({"agent", "function", "custom", "unknown"})


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RelayPluginConfig:
    """Component-local configuration, validated once at registration."""

    rest_url: str = DEFAULT_REST_URL
    timeout: float = 10.0
    agent_name: str = "relay-run"
    checkin_every_tool_calls: int = 20
    enforce: bool = True
    fail_closed: bool = False
    record_tool_failures: bool = True
    subagent_identities: bool = False
    guardrail_priority: int = 100

    _TYPES = {
        "rest_url": str,
        "timeout": (int, float),
        "agent_name": str,
        "checkin_every_tool_calls": int,
        "enforce": bool,
        "fail_closed": bool,
        "record_tool_failures": bool,
        "subagent_identities": bool,
        "guardrail_priority": int,
    }

    @classmethod
    def diagnostics(cls, raw: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """Return Relay ``ConfigDiagnostic`` dictionaries for ``raw``.

        An unknown key is a warning; a wrong type or an out-of-range value is
        an error, which Relay treats as blocking initialization.
        """
        out: list[dict[str, Any]] = []
        for key, value in dict(raw or {}).items():
            expected = cls._TYPES.get(key)
            if expected is None:
                out.append(
                    {
                        "level": "warning",
                        "code": "unitares.unknown_field",
                        "message": f"unknown field {key!r} is ignored",
                        "field": key,
                    }
                )
                continue
            # bool is an int subclass; keep the two apart in both directions.
            bad_type = (
                isinstance(value, bool) and expected is not bool
            ) or not isinstance(value, expected)
            if bad_type:
                out.append(
                    {
                        "level": "error",
                        "code": "unitares.invalid_type",
                        "message": f"{key!r} must be {_type_name(expected)}",
                        "field": key,
                    }
                )
                continue
            if key == "checkin_every_tool_calls" and value < 1:
                out.append(
                    {
                        "level": "error",
                        "code": "unitares.invalid_value",
                        "message": "'checkin_every_tool_calls' must be at least 1",
                        "field": key,
                    }
                )
            if key == "timeout" and value <= 0:
                out.append(
                    {
                        "level": "error",
                        "code": "unitares.invalid_value",
                        "message": "'timeout' must be positive",
                        "field": key,
                    }
                )
        return out

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> RelayPluginConfig:
        """Build a config, raising ``ValueError`` on any error diagnostic."""
        errors = [d for d in cls.diagnostics(raw) if d["level"] == "error"]
        if errors:
            raise ValueError("; ".join(d["message"] for d in errors))
        known = {k: v for k, v in dict(raw or {}).items() if k in cls._TYPES}
        return cls(**known)


def _type_name(expected: Any) -> str:
    if isinstance(expected, tuple):
        return " or ".join(t.__name__ for t in expected)
    return expected.__name__


# --------------------------------------------------------------------------
# Run state
# --------------------------------------------------------------------------


@dataclass
class RunRecord:
    """One Relay run and the UNITARES identity that stands for it."""

    scope_uuid: str
    name: str
    client: Any
    bound: bool = False
    agent_uuid: str | None = None
    client_session_id: str | None = None
    parent_run: str | None = None
    tool_calls: int = 0
    tool_failures: int = 0
    guardrail_rejections: int = 0
    llm_calls: int = 0
    since_checkin: int = 0
    checkins: int = 0
    verdict: str = "proceed"
    guidance: str | None = None
    recent_tools: deque[str] = field(default_factory=lambda: deque(maxlen=5))
    ended: bool = False

    @property
    def blocked(self) -> bool:
        return self.verdict in BLOCKING_VERDICTS


def substrate_summary(run: RunRecord, *, reason: str, status: str | None = None) -> str:
    """Render the run's shape as the text of a substrate check-in.

    This is what the integration can see: counts and names. It carries no
    claim about whether the work was good, which is why the check-in is filed
    under ``substrate_interpretation`` and without a confidence.
    """
    parts = [
        f"relay substrate reading ({reason}) for run '{run.name}':",
        f"{run.tool_calls} tool calls",
        f"{run.tool_failures} failed",
        f"{run.guardrail_rejections} rejected by guardrails",
        f"{run.llm_calls} llm calls",
    ]
    if run.recent_tools:
        parts.append("recent tools: " + ", ".join(run.recent_tools))
    if status:
        parts.append(f"run status {status}")
    return "; ".join(parts)


def _complexity(run: RunRecord) -> float:
    """A bounded shape proxy for the ``complexity`` field, not a judgement."""
    failure_ratio = run.tool_failures / run.tool_calls if run.tool_calls else 0.0
    return round(min(1.0, 0.1 + 0.02 * run.since_checkin + 0.4 * failure_ratio), 3)


def _is_own_gate(guardrail_name: Any, reason: Any) -> bool:
    """True when a rejection came from this module's own policy gate.

    Relay records the registered guardrail name on the scope, possibly
    namespaced by the plugin component, so the name is matched by suffix and
    the rejection message by the prefix every gate message carries.
    """
    if isinstance(guardrail_name, str) and guardrail_name.endswith(GATE_NAME):
        return True
    return isinstance(reason, str) and reason.startswith(GATE_MESSAGE_PREFIX)


# --------------------------------------------------------------------------
# Exporter: the state machine (no threads, no Relay import)
# --------------------------------------------------------------------------


class RelayGovernanceExporter:
    """Fold Relay lifecycle events into UNITARES identities, check-ins and outcomes.

    ``ingest`` takes the dictionary form of a Relay event (``event.to_dict()``)
    and is safe to call from one worker thread. ``gate_for_scope`` may be
    called from any thread and never blocks on the network.
    """

    def __init__(
        self,
        client_factory: Callable[[], Any],
        config: RelayPluginConfig | None = None,
    ) -> None:
        self._client_factory = client_factory
        self.config = config or RelayPluginConfig()
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}
        self._run_of: dict[str, str] = {}

    # -- read side -------------------------------------------------------

    def run_for_scope(self, scope_uuid: str | None) -> RunRecord | None:
        with self._lock:
            run_id = self._run_of.get(scope_uuid or "")
            return self._runs.get(run_id) if run_id else None

    def gate_for_scope(
        self,
        scope_uuid: str | None,
        tool_name: str,
        *,
        parent_uuid: str | None = None,
    ) -> str | None:
        """Return ``None`` to allow ``tool_name`` or a rejection message."""
        run = self.run_for_scope(scope_uuid) or self.run_for_scope(parent_uuid)
        if run is None:
            if self.config.fail_closed:
                return (
                    f"{GATE_MESSAGE_PREFIX} gate: tool '{tool_name}' refused because this "
                    "scope is not bound to a governed run yet (fail_closed=true)"
                )
            return None
        if not run.blocked:
            return None
        guidance = f" Guidance: {run.guidance.rstrip('.')}." if run.guidance else ""
        who = run.agent_uuid or "unbound"
        return (
            f"{GATE_MESSAGE_PREFIX} '{run.verdict}' is in force for run '{run.name}' "
            f"(agent {who}); tool '{tool_name}' refused.{guidance} "
            "This refusal is contestable: read the evidence with "
            "check_working_state and open a review with request_review."
        )

    # -- write side ------------------------------------------------------

    def ingest(self, payload: Mapping[str, Any]) -> None:
        """Consume one Relay event dictionary. Never raises."""
        try:
            kind = payload.get("kind")
            if kind == "scope":
                self._ingest_scope(payload)
        except Exception:  # noqa: BLE001 - a subscriber is infallible by contract
            logger.exception("[unitares/relay] event ingest failed")

    def _ingest_scope(self, p: Mapping[str, Any]) -> None:
        uuid = p.get("uuid")
        if not isinstance(uuid, str):
            return
        category = p.get("category")
        parent = p.get("parent_uuid")
        if p.get("scope_category") == "start":
            with self._lock:
                run_id = self._run_of.get(parent) if isinstance(parent, str) else None
            if run_id is None:
                if category in RUN_ROOT_CATEGORIES:
                    self._start_run(uuid, str(p.get("name") or "run"))
            elif category == "agent" and self.config.subagent_identities:
                self._start_run(uuid, str(p.get("name") or "agent"), parent_run=run_id)
            else:
                with self._lock:
                    self._run_of[uuid] = run_id
            return
        if p.get("scope_category") != "end":
            return
        with self._lock:
            run_id = self._run_of.get(uuid)
            run = self._runs.get(run_id) if run_id else None
        if run is None:
            return
        metadata = p.get("metadata") or {}
        status = metadata.get("otel.status_code") if isinstance(metadata, Mapping) else None
        if category == "tool":
            self._tool_ended(run, p, metadata if isinstance(metadata, Mapping) else {}, status)
        elif category == "llm":
            run.llm_calls += 1
        elif category == "guardrail":
            self._guardrail_ended(run, p)
        if uuid == run.scope_uuid:
            self._end_run(run, status)

    def _start_run(self, scope_uuid: str, name: str, *, parent_run: str | None = None) -> None:
        run = RunRecord(scope_uuid=scope_uuid, name=name, client=None, parent_run=parent_run)
        with self._lock:
            self._runs[scope_uuid] = run
            self._run_of[scope_uuid] = scope_uuid
            parent = self._runs.get(parent_run) if parent_run else None
        try:
            run.client = self._client_factory()
            kwargs: dict[str, Any] = {
                "model_type": "nemo_relay_run",
                "client_hint": "nemo-relay",
                "force_new": True,
            }
            if parent is not None and parent.agent_uuid:
                kwargs["parent_agent_id"] = parent.agent_uuid
                kwargs["spawn_reason"] = "subagent"
            result = run.client.onboard(f"{self.config.agent_name}:{name}", **kwargs)
            run.agent_uuid = getattr(result, "uuid", None)
            run.client_session_id = getattr(result, "client_session_id", None)
            run.verdict = getattr(result, "verdict", None) or "proceed"
            run.guidance = getattr(result, "guidance", None)
            run.bound = True
        except Exception:  # noqa: BLE001
            logger.exception("[unitares/relay] onboarding run %r failed; run stays unbound", name)

    def _tool_ended(
        self,
        run: RunRecord,
        p: Mapping[str, Any],
        metadata: Mapping[str, Any],
        status: str | None,
    ) -> None:
        name = str(p.get("name") or "tool")
        run.tool_calls += 1
        run.since_checkin += 1
        run.recent_tools.append(name)
        if status == "ERROR":
            run.tool_failures += 1
            if self.config.record_tool_failures:
                self._record_outcome(
                    run,
                    "task_failed",
                    {
                        "kind": "tool_error",
                        "tool": name,
                        "error_type": metadata.get("error.type"),
                        "exception_type": metadata.get("exception.type"),
                        "description": metadata.get("otel.status_description"),
                    },
                )
        if run.since_checkin >= self.config.checkin_every_tool_calls:
            self._checkin(run, reason="cadence")

    def _guardrail_ended(self, run: RunRecord, p: Mapping[str, Any]) -> None:
        data = p.get("data") or {}
        if not isinstance(data, Mapping) or not data.get("rejected"):
            return
        run.guardrail_rejections += 1
        if _is_own_gate(p.get("name"), data.get("rejection_reason")):
            # Our own enforcement is not evidence about the work.
            return
        if self.config.record_tool_failures:
            self._record_outcome(
                run,
                "tool_rejected",
                {
                    "kind": "guardrail_rejection",
                    "guardrail": p.get("name"),
                    "reason": data.get("rejection_reason"),
                },
            )

    def _checkin(self, run: RunRecord, *, reason: str, status: str | None = None) -> None:
        if not run.bound:
            return
        try:
            result = run.client.checkin(
                substrate_summary(run, reason=reason, status=status),
                complexity=_complexity(run),
                epistemic_class=SUBSTRATE_EPISTEMIC_CLASS,
                response_mode="compact",
            )
            verdict = getattr(result, "verdict", None)
            with self._lock:
                if verdict:
                    run.verdict = str(verdict)
                run.guidance = getattr(result, "guidance", None)
                run.since_checkin = 0
                run.checkins += 1
        except Exception:  # noqa: BLE001
            logger.exception("[unitares/relay] check-in for run %r failed; policy unchanged", run.name)

    def _record_outcome(self, run: RunRecord, outcome_type: str, detail: dict[str, Any]) -> None:
        if not run.bound:
            return
        try:
            run.client.call_tool(
                "outcome_event",
                {
                    "outcome_type": outcome_type,
                    "is_bad": True,
                    "detail": {k: v for k, v in detail.items() if v is not None},
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("[unitares/relay] outcome %s for run %r failed", outcome_type, run.name)

    def _end_run(self, run: RunRecord, status: str | None) -> None:
        if run.ended:
            return
        run.ended = True
        self._checkin(run, reason="run end", status=status)
        if status == "ERROR" and self.config.record_tool_failures:
            self._record_outcome(run, "task_failed", {"kind": "run_error", "run": run.name})
        close = getattr(run.client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                logger.debug("[unitares/relay] client close failed", exc_info=True)
        with self._lock:
            self._runs.pop(run.scope_uuid, None)
            for scope, owner in list(self._run_of.items()):
                if owner == run.scope_uuid:
                    del self._run_of[scope]


# --------------------------------------------------------------------------
# Worker thread
# --------------------------------------------------------------------------


class _Barrier:
    __slots__ = ("event",)

    def __init__(self) -> None:
        self.event = threading.Event()


class RelayExportWorker:
    """Drain event dictionaries onto a handler from one daemon thread."""

    def __init__(self, handler: Callable[[Mapping[str, Any]], None], *, name: str = "unitares-relay-export") -> None:
        self._handler = handler
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._closed = False

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def submit(self, payload: Mapping[str, Any]) -> None:
        if not self._closed:
            self._queue.put(payload)

    def flush(self, timeout: float | None = 5.0) -> bool:
        """Block until everything queued before the call has been handled."""
        if self._closed or not self._thread.is_alive():
            return True
        barrier = _Barrier()
        self._queue.put(barrier)
        return barrier.event.wait(timeout)

    def close(self, timeout: float | None = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if isinstance(item, _Barrier):
                item.event.set()
                continue
            try:
                self._handler(item)
            except Exception:  # noqa: BLE001
                logger.exception("[unitares/relay] worker handler failed")


# --------------------------------------------------------------------------
# The Relay plugin
# --------------------------------------------------------------------------


def _default_client_factory(config: RelayPluginConfig) -> Callable[[], Any]:
    def factory() -> Any:
        from unitares_sdk.sync_client import SyncGovernanceClient

        return SyncGovernanceClient(rest_url=config.rest_url, timeout=config.timeout, transport="rest")

    return factory


class UnitaresRelayPlugin:
    """A ``nemo_relay.plugin.Plugin`` implementation.

    ``client_factory`` is called once per Relay run and must return an object
    with ``onboard``, ``checkin`` and ``call_tool`` in the shape of
    ``SyncGovernanceClient``; tests inject fakes here. Left ``None``, a REST
    client is built from the component config.
    """

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory
        self.config: RelayPluginConfig | None = None
        self.exporter: RelayGovernanceExporter | None = None
        self._worker: RelayExportWorker | None = None

    # Plugin protocol -----------------------------------------------------

    def validate(self, plugin_config: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        diagnostics = RelayPluginConfig.diagnostics(plugin_config)
        return diagnostics or None

    def register(self, plugin_config: Mapping[str, Any], context: Any) -> None:
        self.close()
        config = RelayPluginConfig.from_mapping(plugin_config)
        factory = self._client_factory or _default_client_factory(config)
        self.config = config
        self.exporter = RelayGovernanceExporter(factory, config)
        self._worker = RelayExportWorker(self.exporter.ingest)
        self._worker.start()
        context.register_subscriber(SUBSCRIBER_NAME, self._on_event)
        if config.enforce:
            context.register_tool_conditional_execution_guardrail(
                GATE_NAME, config.guardrail_priority, self._gate
            )

    # Callbacks -----------------------------------------------------------

    def _on_event(self, event: Any) -> None:
        worker = self._worker
        if worker is None:
            return
        try:
            payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
            worker.submit(payload)
        except Exception:  # noqa: BLE001
            logger.exception("[unitares/relay] could not enqueue event")

    def _gate(self, tool_name: str, args: Any) -> str | None:
        del args  # arguments are not read; the gate is per run, not per call
        exporter = self.exporter
        if exporter is None:
            return None
        try:
            from nemo_relay import scope as relay_scope

            handle = relay_scope.get_handle()
            return exporter.gate_for_scope(
                getattr(handle, "uuid", None),
                tool_name,
                parent_uuid=getattr(handle, "parent_uuid", None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[unitares/relay] policy gate could not resolve the current scope")
            if exporter.config.fail_closed:
                return f"{GATE_MESSAGE_PREFIX} gate unavailable: {exc}"
            return None

    # Lifecycle -----------------------------------------------------------

    def flush(self, timeout: float | None = 5.0) -> bool:
        """Wait for queued events to reach the server; returns False on timeout."""
        return self._worker.flush(timeout) if self._worker else True

    def close(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None


def install(client_factory: Callable[[], Any] | None = None, *, kind: str = PLUGIN_KIND) -> UnitaresRelayPlugin:
    """Register the plugin kind with Relay and return the plugin object.

    Activate it afterwards with ``nemo_relay.plugin.initialize`` and a
    ``ComponentSpec(kind=kind, config={...})``. Requires the ``nemo-relay``
    extra; the import error names it.
    """
    try:
        from nemo_relay import plugin as relay_plugin
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "unitares_sdk.integrations.nemo_relay needs the optional 'nemo-relay' extra: "
            "pip install 'unitares-sdk[nemo-relay]'"
        ) from exc
    plugin = UnitaresRelayPlugin(client_factory)
    relay_plugin.register(kind, plugin)
    return plugin
