"""Typed internal contract shared by advisory inference entry points.

Public MCP handlers own transport envelopes, signatures, and recovery wording.
The underlying inference services return this transport-neutral result so one
public tool never has to invoke or parse another public tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
