"""Typed internal contract shared by advisory inference entry points.

Public MCP handlers own transport envelopes, signatures, and recovery wording.
The underlying inference services return this transport-neutral result so one
public tool never has to invoke or parse another public tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from src.logging_utils import get_logger

logger = get_logger(__name__)

_INFERENCE_CALL_RECORD_SCHEMA = "unitares.inference_call.v1"

# Route fields kept on a direct call_model / delegate_inference audit row.
# Prompt text, response text, hashes, raw base_url, warnings, and raw
# finish_reason never enter this record.
_INFERENCE_CALL_ROUTE_FIELDS = (
    "host_id",
    "provider_kind",
    "transport",
    "model_used",
    "models_used",
    "model_requested",
    "task_type",
    "privacy_class",
    "cost_class",
    "cost_usd",
    "orchestrator_execution_id",
    "orchestrator_agent_id",
    "latency_ms",
    "tokens_used",
    "model_reporting_status",
)
_IDENTIFIER_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_CODE_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _safe_identifier(value: Any) -> Any:
    """Keep numbers/booleans and identifier-shaped strings; scrub free text."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_identifier(item) for item in value]
    text = str(value)
    if _IDENTIFIER_SHAPE.fullmatch(text):
        return text
    return "unrecorded_text"


def _safe_code(value: Any) -> str:
    """Keep snake_case error/category codes; scrub arbitrary backend text."""
    if isinstance(value, str) and _CODE_SHAPE.fullmatch(value):
        return value
    return "unrecorded_text"


def build_inference_call_record(
    tool: str,
    outcome: "InferenceOutcome",
) -> dict[str, Any]:
    """Build a text-free route-level audit record for a raw inference tool call.

    ``consult`` writes ``event_type='consultation'`` with caller-keyed HMAC
    hashes. Direct ``call_model`` and ``delegate_inference`` calls return no
    hash key in their wire contract, so their audit record (`event_type=
    'inference_call'`) keeps only route, privacy class, and outcome metadata —
    never prompt text, response text, raw base URLs, or unvalidated output
    excerpts.
    """
    record: dict[str, Any] = {
        "schema": _INFERENCE_CALL_RECORD_SCHEMA,
        "tool": _safe_code(tool),
        "status": "completed" if outcome.ok else "failed",
    }
    if outcome.ok:
        route: dict[str, Any] = {}
        for name in _INFERENCE_CALL_ROUTE_FIELDS:
            if outcome.inference.get(name) is not None:
                route[name] = _safe_identifier(outcome.inference[name])
        if outcome.routed_via is not None:
            route["routed_via"] = _safe_identifier(outcome.routed_via)
        if route:
            record["route"] = route
            if route.get("privacy_class") is not None:
                record["privacy_class"] = route["privacy_class"]
        return record

    failure = outcome.failure
    assert failure is not None
    failure_record: dict[str, Any] = {
        "code": _safe_code(failure.code),
        "category": _safe_code(failure.category),
        "execution_started": bool(failure.execution_started),
        "possibly_running": bool(failure.possibly_running),
    }
    details = failure.details or {}
    # Standard-lane failures attach route keys only inside the catch around the
    # provider request; delegated failures attach them on preflight too, so gate
    # delegated route attribution on execution_started (#2483 P3).
    include_route = tool == "call_model" or bool(failure.execution_started)
    if include_route:
        failed_route = {
            name: _safe_identifier(details[name])
            for name in ("host_id", "provider_kind", "privacy_class", "task_type")
            if details.get(name) is not None
        }
        if failed_route:
            record["route"] = failed_route
            if failed_route.get("privacy_class") is not None:
                record["privacy_class"] = failed_route["privacy_class"]
    record["failure"] = failure_record
    return record


def record_inference_call(
    tool: str,
    agent_id: str | None,
    outcome: "InferenceOutcome",
) -> None:
    """Append a route-level ``inference_call`` audit event (non-fatal)."""
    try:
        from src.audit_log import audit_logger

        audit_logger.log_inference_call(
            agent_id=agent_id,
            record=build_inference_call_record(tool, outcome),
        )
    except Exception as exc:  # noqa: BLE001 - audit logging must never fail the call
        logger.warning("inference_call audit record failed (non-fatal): %s", exc)


@dataclass(frozen=True, slots=True)
class InferenceFailure:
    """One terminal failure from an inference service."""

    message: str
    code: str
    category: str
    details: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    execution_started: bool = False
    possibly_running: bool = False


@dataclass(frozen=True, slots=True)
class InferenceOutcome:
    """Transport-neutral advisory inference outcome.

    ``inference`` is the normalized ``unitares.inference_result.v0`` evidence
    record. Provider stdout, public response wrappers, and agent signatures do
    not belong in this internal contract.
    """

    response: str = ""
    inference: dict[str, Any] = field(default_factory=dict)
    routed_via: str | None = None
    task_type: str | None = None
    model_used: Any = None
    models_used: tuple[str, ...] = ()
    tokens_used: int = 0
    energy_cost: float = 0.0
    message: str = ""
    failure: InferenceFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    @classmethod
    def failed(
        cls,
        message: str,
        *,
        code: str,
        category: str,
        details: dict[str, Any] | None = None,
        recovery: dict[str, Any] | None = None,
        execution_started: bool = False,
        possibly_running: bool = False,
    ) -> "InferenceOutcome":
        return cls(
            failure=InferenceFailure(
                message=message,
                code=code,
                category=category,
                details=dict(details or {}),
                recovery=dict(recovery or {}),
                execution_started=execution_started,
                possibly_running=possibly_running,
            )
        )
