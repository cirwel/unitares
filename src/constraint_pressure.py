"""Pure, non-authoritative constraint-pressure dynamics for offline replay.

This module deliberately has no governance-monitor, persistence, or policy
integration.  It evolves an observer state over a caller-supplied one-sided
constraint residual.  Missing observations hold state rather than pretending
that an unobserved constraint is satisfied.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Mapping


SCHEMA_VERSION = "constraint-pressure.v0"


def _require_finite_nonnegative(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


@dataclass(frozen=True)
class ConstraintPressureConfig:
    """Versioned parameters for one named constraint.

    ``accumulation_rate`` has units of pressure per residual-second and
    ``leak_rate`` has units of inverse seconds.  No defaults are supplied: a
    replay manifest must make every choice explicit.
    """

    constraint_id: str
    accumulation_rate: float
    leak_rate: float
    max_pressure: float

    def __post_init__(self) -> None:
        if not self.constraint_id or not self.constraint_id.strip():
            raise ValueError("constraint_id must be non-empty")
        object.__setattr__(
            self,
            "accumulation_rate",
            _require_finite_nonnegative("accumulation_rate", self.accumulation_rate),
        )
        object.__setattr__(
            self,
            "leak_rate",
            _require_finite_nonnegative("leak_rate", self.leak_rate),
        )
        max_pressure = _require_finite_nonnegative("max_pressure", self.max_pressure)
        if max_pressure == 0.0:
            raise ValueError("max_pressure must be greater than zero")
        object.__setattr__(self, "max_pressure", max_pressure)

    def manifest(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}

    @property
    def manifest_digest(self) -> str:
        encoded = json.dumps(
            self.manifest(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ConstraintPressureState:
    """Checkpointable observer state for one constraint."""

    pressure: float = 0.0
    observed_seconds: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "pressure", _require_finite_nonnegative("pressure", self.pressure)
        )
        object.__setattr__(
            self,
            "observed_seconds",
            _require_finite_nonnegative("observed_seconds", self.observed_seconds),
        )

    def to_dict(self) -> dict[str, float | str]:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ConstraintPressureState:
        version = value.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported constraint-pressure schema: {version!r}")
        return cls(
            pressure=value["pressure"],
            observed_seconds=value["observed_seconds"],
        )


@dataclass(frozen=True)
class ConstraintPressureTransition:
    """One replay transition plus the provenance needed to interpret it."""

    state: ConstraintPressureState
    residual: float | None
    dt_seconds: float
    observed: bool
    resolved: bool
    saturated: bool
    manifest_digest: str


def advance_constraint_pressure(
    state: ConstraintPressureState,
    config: ConstraintPressureConfig,
    *,
    residual: float | None,
    dt_seconds: float,
    resolved: bool = False,
) -> ConstraintPressureTransition:
    """Advance pressure using an exact constant-input leaky-integrator step.

    ``residual`` must already be the one-sided violation ``max(0, g(x))``.
    ``None`` means the constraint was not observed and holds pressure unchanged,
    including across elapsed wall time.  ``resolved`` is an independently
    observed closure event and clears pressure; combining it with a residual is
    rejected as ambiguous.
    """

    dt_seconds = _require_finite_nonnegative("dt_seconds", dt_seconds)
    if resolved and residual is not None:
        raise ValueError("resolved and residual cannot be supplied together")

    if resolved:
        return ConstraintPressureTransition(
            state=ConstraintPressureState(
                pressure=0.0,
                observed_seconds=state.observed_seconds,
            ),
            residual=None,
            dt_seconds=dt_seconds,
            observed=True,
            resolved=True,
            saturated=False,
            manifest_digest=config.manifest_digest,
        )

    if residual is None:
        return ConstraintPressureTransition(
            state=state,
            residual=None,
            dt_seconds=dt_seconds,
            observed=False,
            resolved=False,
            saturated=False,
            manifest_digest=config.manifest_digest,
        )

    residual = _require_finite_nonnegative("residual", residual)
    if config.leak_rate == 0.0:
        raw_pressure = state.pressure + config.accumulation_rate * residual * dt_seconds
    else:
        exponent = -config.leak_rate * dt_seconds
        decay = math.exp(exponent)
        drive = (
            config.accumulation_rate
            * residual
            * (-math.expm1(exponent))
            / config.leak_rate
        )
        raw_pressure = decay * state.pressure + drive

    pressure = min(config.max_pressure, raw_pressure)
    return ConstraintPressureTransition(
        state=ConstraintPressureState(
            pressure=pressure,
            observed_seconds=state.observed_seconds + dt_seconds,
        ),
        residual=residual,
        dt_seconds=dt_seconds,
        observed=True,
        resolved=False,
        saturated=raw_pressure >= config.max_pressure,
        manifest_digest=config.manifest_digest,
    )
