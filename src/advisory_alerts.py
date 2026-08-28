"""Pure, shadow-only advisory alert decisions.

This module deliberately stops before logging, response formatting, transport,
or delivery.  It separates a measured condition from an alert candidate and
records why nothing was delivered.  Integrations may persist the returned
record, but they must not treat it as permission to alter an agent response.

Only one environment switch is recognized: ``UNITARES_ADVISORY_MODE``.  The
closed mode set is ``off|shadow`` and defaults to ``off``.  Unknown values,
including a future-looking ``surface``, fail closed to ``off`` with typed
degradation metadata.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import math
import os
from typing import Any


ADVISORY_MODE_ENV = "UNITARES_ADVISORY_MODE"
ADVISORY_SCHEMA_VERSION = "unitares.advisory-decision.v1"
ADVISORY_CORE_VERSION = "shadow-only-v1"


class AdvisoryMode(StrEnum):
    """The complete v1 mode set; live surfacing is intentionally absent."""

    OFF = "off"
    SHADOW = "shadow"


class EligibilityStatus(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    NOT_ELIGIBLE = "not_eligible"
    ELIGIBLE = "eligible"


class SuppressionReason(StrEnum):
    MASTER_BYPASS = "master_bypass"
    NOVELTY_DEDUPLICATED = "novelty_deduplicated"
    RATE_LIMITED = "rate_limited"
    ATTENTION_BUDGET_EXHAUSTED = "attention_budget_exhausted"
    OPERATOR_SUPPRESSED = "operator_suppressed"


class DegradationReason(StrEnum):
    INVALID_MODE = "invalid_mode"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    TIMEOUT = "timeout"
    PARTIAL_MEASUREMENT = "partial_measurement"


class DeliveryStatus(StrEnum):
    """V1 has no delivered state by construction."""

    NOT_DELIVERED = "not_delivered"


class DeliveryReason(StrEnum):
    MODE_OFF = "mode_off"
    SHADOW_ONLY = "shadow_only"
    NOT_ELIGIBLE = "not_eligible"
    SUPPRESSED = "suppressed"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class SuppressionMetadata:
    suppressed: bool = False
    reason: SuppressionReason | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.suppressed != (self.reason is not None):
            raise ValueError(
                "suppression reason must be present exactly when suppressed is true"
            )

    @classmethod
    def active(
        cls,
        reason: SuppressionReason,
        *,
        detail: str | None = None,
    ) -> SuppressionMetadata:
        return cls(suppressed=True, reason=reason, detail=detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suppressed": self.suppressed,
            "reason": self.reason.value if self.reason is not None else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DegradationMetadata:
    degraded: bool = False
    reason: DegradationReason | None = None
    retryable: bool = False
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.degraded != (self.reason is not None):
            raise ValueError(
                "degradation reason must be present exactly when degraded is true"
            )
        if self.retryable and not self.degraded:
            raise ValueError("a non-degraded decision cannot be retryable")

    @classmethod
    def active(
        cls,
        reason: DegradationReason,
        *,
        retryable: bool,
        detail: str | None = None,
    ) -> DegradationMetadata:
        return cls(
            degraded=True,
            reason=reason,
            retryable=retryable,
            detail=detail,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "degraded": self.degraded,
            "reason": self.reason.value if self.reason is not None else None,
            "retryable": self.retryable,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DeliveryMetadata:
    status: DeliveryStatus
    reason: DeliveryReason

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status.value, "reason": self.reason.value}


@dataclass(frozen=True)
class AdvisoryModeResolution:
    mode: AdvisoryMode
    degradation: DegradationMetadata = DegradationMetadata()


@dataclass(frozen=True)
class AdvisoryCandidate:
    """Content-addressed candidate produced by an eligible shadow decision."""

    candidate_id: str
    replay_id: str
    candidate_type: str
    subject: dict[str, Any]
    measurement: dict[str, Any]
    policy: dict[str, Any]
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "replay_id": self.replay_id,
            "candidate_type": self.candidate_type,
            "subject": _json_copy(self.subject),
            "measurement": _json_copy(self.measurement),
            "policy": _json_copy(self.policy),
            "provenance": _json_copy(self.provenance),
        }


@dataclass(frozen=True)
class AdvisoryDecision:
    """A replayable policy record that can never represent live delivery."""

    mode: AdvisoryMode
    eligibility: EligibilityStatus
    replay_id: str | None
    candidate: AdvisoryCandidate | None
    suppression: SuppressionMetadata
    degradation: DegradationMetadata
    delivery: DeliveryMetadata
    schema_version: str = ADVISORY_SCHEMA_VERSION
    core_version: str = ADVISORY_CORE_VERSION

    @property
    def candidate_id(self) -> str | None:
        return self.candidate.candidate_id if self.candidate is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "core_version": self.core_version,
            "mode": self.mode.value,
            "eligibility": self.eligibility.value,
            "replay_id": self.replay_id,
            "candidate_id": self.candidate_id,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "suppression": self.suppression.to_dict(),
            "degradation": self.degradation.to_dict(),
            "delivery": self.delivery.to_dict(),
        }


def resolve_advisory_mode(
    environ: Mapping[str, str] | None = None,
) -> AdvisoryModeResolution:
    """Resolve the single master switch, failing closed on unknown values."""

    source = os.environ if environ is None else environ
    raw = source.get(ADVISORY_MODE_ENV, AdvisoryMode.OFF.value)
    normalized = str(raw).strip().lower() or AdvisoryMode.OFF.value
    if normalized == AdvisoryMode.OFF.value:
        return AdvisoryModeResolution(mode=AdvisoryMode.OFF)
    if normalized == AdvisoryMode.SHADOW.value:
        return AdvisoryModeResolution(mode=AdvisoryMode.SHADOW)
    return AdvisoryModeResolution(
        mode=AdvisoryMode.OFF,
        degradation=DegradationMetadata.active(
            DegradationReason.INVALID_MODE,
            retryable=False,
            detail=f"unsupported {ADVISORY_MODE_ENV} value",
        ),
    )


def decide_advisory(
    *,
    candidate_type: str,
    subject: Mapping[str, Any],
    measurement: Mapping[str, Any],
    policy: Mapping[str, Any],
    eligible: bool | None,
    provenance: Mapping[str, Any] | None = None,
    suppression: SuppressionMetadata | None = None,
    degradation: DegradationMetadata | None = None,
    environ: Mapping[str, str] | None = None,
) -> AdvisoryDecision:
    """Return an off/shadow decision without any delivery side effect.

    ``off`` returns before validating or hashing candidate inputs.  That early
    return is the master bypass: integrations can call this function at their
    normal decision seam without spending candidate/replay work when advisory
    evaluation is disabled.
    """

    mode_resolution = resolve_advisory_mode(environ)
    no_suppression = SuppressionMetadata()
    no_degradation = DegradationMetadata()

    if mode_resolution.mode is AdvisoryMode.OFF:
        return AdvisoryDecision(
            mode=AdvisoryMode.OFF,
            eligibility=EligibilityStatus.NOT_EVALUATED,
            replay_id=None,
            candidate=None,
            suppression=SuppressionMetadata.active(
                SuppressionReason.MASTER_BYPASS
            ),
            degradation=mode_resolution.degradation,
            delivery=DeliveryMetadata(
                status=DeliveryStatus.NOT_DELIVERED,
                reason=DeliveryReason.MODE_OFF,
            ),
        )

    suppression = suppression or no_suppression
    degradation = degradation or no_degradation
    if suppression.suppressed and degradation.degraded:
        raise ValueError("a decision cannot be both suppressed and degraded")
    if degradation.degraded:
        if eligible is not None:
            raise ValueError("a degraded evaluation must use eligible=None")
    elif not isinstance(eligible, bool):
        raise TypeError("eligible must be a bool for a non-degraded evaluation")
    if suppression.suppressed and eligible is not True:
        raise ValueError("only an eligible candidate can be suppressed")

    normalized = _normalize_candidate_inputs(
        candidate_type=candidate_type,
        subject=subject,
        measurement=measurement,
        policy=policy,
        provenance=provenance or {},
    )
    replay_id = _content_id("ar", {
        "schema_version": ADVISORY_SCHEMA_VERSION,
        "core_version": ADVISORY_CORE_VERSION,
        # Eligibility is the policy result, not a measurement.  Keeping it in
        # the replay basis makes a changed policy result produce a changed
        # replay id while suppression and delivery remain downstream metadata.
        "eligible": eligible,
        **normalized,
    })

    if degradation.degraded:
        return AdvisoryDecision(
            mode=AdvisoryMode.SHADOW,
            eligibility=EligibilityStatus.NOT_EVALUATED,
            replay_id=replay_id,
            candidate=None,
            suppression=no_suppression,
            degradation=degradation,
            delivery=DeliveryMetadata(
                status=DeliveryStatus.NOT_DELIVERED,
                reason=DeliveryReason.DEGRADED,
            ),
        )

    if eligible is False:
        return AdvisoryDecision(
            mode=AdvisoryMode.SHADOW,
            eligibility=EligibilityStatus.NOT_ELIGIBLE,
            replay_id=replay_id,
            candidate=None,
            suppression=no_suppression,
            degradation=no_degradation,
            delivery=DeliveryMetadata(
                status=DeliveryStatus.NOT_DELIVERED,
                reason=DeliveryReason.NOT_ELIGIBLE,
            ),
        )

    candidate_id = _content_id("ac", {
        "schema_version": ADVISORY_SCHEMA_VERSION,
        "replay_id": replay_id,
        "eligible": True,
    })
    candidate = AdvisoryCandidate(
        candidate_id=candidate_id,
        replay_id=replay_id,
        candidate_type=normalized["candidate_type"],
        subject=normalized["subject"],
        measurement=normalized["measurement"],
        policy=normalized["policy"],
        provenance=normalized["provenance"],
    )
    delivery_reason = (
        DeliveryReason.SUPPRESSED
        if suppression.suppressed
        else DeliveryReason.SHADOW_ONLY
    )
    return AdvisoryDecision(
        mode=AdvisoryMode.SHADOW,
        eligibility=EligibilityStatus.ELIGIBLE,
        replay_id=replay_id,
        candidate=candidate,
        suppression=suppression,
        degradation=no_degradation,
        delivery=DeliveryMetadata(
            status=DeliveryStatus.NOT_DELIVERED,
            reason=delivery_reason,
        ),
    )


def _normalize_candidate_inputs(
    *,
    candidate_type: str,
    subject: Mapping[str, Any],
    measurement: Mapping[str, Any],
    policy: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_type = str(candidate_type).strip()
    if not normalized_type:
        raise ValueError("candidate_type must be a non-empty string")
    for name, value in (
        ("subject", subject),
        ("measurement", measurement),
        ("policy", policy),
        ("provenance", provenance),
    ):
        if not isinstance(value, Mapping):
            raise TypeError(f"{name} must be a mapping")
    return {
        "candidate_type": normalized_type,
        "subject": _normalize_json(subject, path="subject"),
        "measurement": _normalize_json(measurement, path="measurement"),
        "policy": _normalize_json(policy, path="policy"),
        "provenance": _normalize_json(provenance, path="provenance"),
    }


def _normalize_json(value: Any, *, path: str) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise TypeError(f"{path} contains a non-string mapping key")
        for key in sorted(keys):
            normalized[key] = _normalize_json(value[key], path=f"{path}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _normalize_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains unsupported type {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _content_id(prefix: str, value: Any) -> str:
    digest = sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


__all__ = [
    "ADVISORY_CORE_VERSION",
    "ADVISORY_MODE_ENV",
    "ADVISORY_SCHEMA_VERSION",
    "AdvisoryCandidate",
    "AdvisoryDecision",
    "AdvisoryMode",
    "AdvisoryModeResolution",
    "DegradationMetadata",
    "DegradationReason",
    "DeliveryMetadata",
    "DeliveryReason",
    "DeliveryStatus",
    "EligibilityStatus",
    "SuppressionMetadata",
    "SuppressionReason",
    "decide_advisory",
    "resolve_advisory_mode",
]
