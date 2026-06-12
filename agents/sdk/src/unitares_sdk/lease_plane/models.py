"""Typed request and response models for the lease-plane HTTP contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonicalize import CANONICAL_SCHEMES, canonicalize

HolderClass: TypeAlias = Literal["process_instance", "substrate_earned"]
HolderKind: TypeAlias = Literal["local_beam", "remote_heartbeat"]
EarnedStatus: TypeAlias = Literal["provisional", "earned"]
ReleaseReason: TypeAlias = Literal[
    "normal",
    "down_local",
    "reaped_after_supervisor_failed",
    "reaped_local_ttl",
    "reaped_remote_ttl",
    "handoff",
    "forced",
]


class LeasePlaneModel(BaseModel):
    """Strict model base for caller-owned request shapes."""

    model_config = ConfigDict(extra="forbid")


def _validate_substrate_pair(
    substrate_state: dict[str, Any] | None,
    substrate_state_observed_at: datetime | None,
    *,
    where: str,
) -> None:
    """Caller-side enforcement of the migration-034 pair-coherence CHECK.

    Catches partial-pair (one set, the other unset) before the request leaves
    the client, so the resulting validation error names the field instead of
    surfacing as an HTTP 422 from the DB CHECK. Mirrors the
    `substrate_state_observed_pair_coherent` CHECK constraint (RFC §7.13.5).
    """
    if (substrate_state is None) != (substrate_state_observed_at is None):
        raise ValueError(
            f"{where}: substrate_state and substrate_state_observed_at must "
            "be both set or both unset (RFC §7.13.5 pair-coherence)"
        )


class LeaseRecord(BaseModel):
    """Active or historical lease row as returned by the lease plane."""

    lease_id: UUID
    surface_id: str = Field(min_length=1)
    surface_kind: str = Field(min_length=1)
    holder_agent_uuid: UUID
    holder_class: HolderClass
    holder_kind: HolderKind
    heartbeat_required: bool
    expires_at: datetime
    original_ttl_s: int = Field(gt=0, le=3600)
    holder_pid: str | None = None
    intent: str | None = None
    acquired_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    released_at: datetime | None = None
    release_reason: ReleaseReason | None = None
    audit_session: str | None = None
    earned_status: EarnedStatus = "provisional"
    # RFC §7.13: resident substrate observation. Optional — only resident:/
    # leases populate these fields. dict (not arbitrary JSON) so non-object
    # values fail validation client-side.
    substrate_state: dict[str, Any] | None = None
    substrate_state_observed_at: datetime | None = None

    @model_validator(mode="after")
    def _holder_kind_matches_heartbeat(self) -> "LeaseRecord":
        expected = self.holder_kind == "remote_heartbeat"
        if self.heartbeat_required is not expected:
            raise ValueError("heartbeat_required must match holder_kind")
        return self

    @model_validator(mode="after")
    def _substrate_pair_coherent(self) -> "LeaseRecord":
        _validate_substrate_pair(
            self.substrate_state,
            self.substrate_state_observed_at,
            where="LeaseRecord",
        )
        return self


class AcquireRequest(LeasePlaneModel):
    """Request body for POST /v1/lease/acquire.

    `surface_kind` is no longer a request field per RFC v0.8 §7.2.3 — it is
    derived server-side from the surface_id scheme prefix via migration 026's
    generated column.

    `surface_id` auto-canonicalizes via field_validator per RFC v0.8 §7.12.5:
    rejects NUL bytes, ?-bearing strings (reserved for v1 modifier form per
    §7.12.4), non-canonical schemes (per §7.2.1 list); auto-canonicalizes
    via `unitares_sdk.lease_plane.canonicalize`.
    """

    surface_id: str = Field(min_length=1)
    holder_agent_uuid: UUID
    holder_class: HolderClass
    holder_kind: HolderKind
    ttl_s: int = Field(gt=0, le=3600)
    holder_pid: str | None = None
    intent: str | None = None
    audit_session: str | None = None
    # RFC §7.13: optional resident substrate observation. Both fields nullable;
    # pair-coherence enforced by model_validator (mirrors migration-034 CHECK).
    # Server-side CHECK also restricts substrate_state to surface_kind='resident'
    # leases — caller-side rejection happens here for resident:/ AcquireRequests
    # only when the substrate fields are actually populated.
    substrate_state: dict[str, Any] | None = None
    substrate_state_observed_at: datetime | None = None

    @field_validator("surface_id", mode="before")
    @classmethod
    def _validate_surface_id(cls, v: str) -> str:
        """Reject invalid scheme, NUL bytes, ?-bearing values; auto-canonicalize."""
        if not isinstance(v, str):
            raise ValueError("surface_id must be a string")
        if "\x00" in v:
            raise ValueError("NUL byte in surface_id")
        if "?" in v:
            raise ValueError(
                "query string in surface_id reserved for v1 modifier form (RFC §7.12.4); "
                "use plain canonical form for v0"
            )
        scheme = v.split(":", 1)[0]
        if scheme not in CANONICAL_SCHEMES:
            raise ValueError(
                f"surface_id scheme {scheme!r} not in canonical scheme list {CANONICAL_SCHEMES} "
                f"(RFC §7.2.1)"
            )
        return canonicalize(v)

    @model_validator(mode="after")
    def _substrate_pair_coherent(self) -> "AcquireRequest":
        _validate_substrate_pair(
            self.substrate_state,
            self.substrate_state_observed_at,
            where="AcquireRequest",
        )
        return self


class RenewRequest(LeasePlaneModel):
    """Request body for POST /v1/lease/renew.

    The renew contract intentionally accepts no ttl_s. The lease plane extends
    by the immutable original_ttl_s stored at acquire time.

    RFC §7.13: optional substrate_state + substrate_state_observed_at fields
    let resident heartbeats refresh the substrate observation alongside the
    lease. Both nullable; pair-coherence enforced client-side.
    """

    lease_id: UUID
    substrate_state: dict[str, Any] | None = None
    substrate_state_observed_at: datetime | None = None

    @model_validator(mode="after")
    def _substrate_pair_coherent(self) -> "RenewRequest":
        _validate_substrate_pair(
            self.substrate_state,
            self.substrate_state_observed_at,
            where="RenewRequest",
        )
        return self


class HeartbeatRequest(RenewRequest):
    """Request body for POST /v1/lease/heartbeat."""


class ReleaseRequest(LeasePlaneModel):
    """Request body for POST /v1/lease/release."""

    lease_id: UUID
    release_reason: ReleaseReason = "normal"


class ForceReleaseRequest(LeasePlaneModel):
    """Request body for POST /v1/lease/force-release (operator-only, §7.10).

    Uses the elevated LEASE_FORCE_RELEASE_TOKEN; the Elixir router rejects any
    other token at the path level. Only `lease_id` is required — the router
    pins release_reason='forced' server-side.
    """

    lease_id: UUID


class HandoffOfferRequest(LeasePlaneModel):
    """Request body for POST /v1/lease/handoff/offer.

    Handoff is a release-and-reacquire pattern: on accept, the old lease row
    is closed with `release_reason='handoff'` and a new lease row is inserted
    for the receiving holder. `original_ttl_s` is immutable per lease_id, so
    in-place handoff is not possible.

    `ttl_s` here is therefore the **new lease's `original_ttl_s`** that the
    receiving holder will hold after accept — not an offer-window TTL. The
    offer-window expiry (how long the offer remains valid for accept) is
    server-internal (Oban handoff-timeout job) and is not part of this
    contract.
    """

    lease_id: UUID
    to_holder_agent_uuid: UUID
    ttl_s: int = Field(gt=0, le=3600)


class HandoffAcceptRequest(LeasePlaneModel):
    """Request body for POST /v1/lease/handoff/accept."""

    handoff_id: UUID


class AcquireOk(BaseModel):
    ok: Literal[True]
    lease: LeaseRecord
    idempotent: bool = False
    drift_warning: list[str] = Field(default_factory=list)


class AcquireHeldByOther(BaseModel):
    ok: Literal[False]
    error: Literal["held_by_other"]
    surface_id: str
    blocking_lease_id: UUID
    held_by_uuid: UUID
    expires_at: datetime
    # Defense-in-depth (PR 5 council fix): retry_after_hint_ms defaults to 0
    # so any emitter that omits the field doesn't degrade to AcquireSchemaInvalid.
    # surface_id and blocking_lease_id remain REQUIRED — missing them is a real
    # contract violation that should surface clearly.
    retry_after_hint_ms: int = 0


class AcquirePermissionDenied(BaseModel):
    ok: Literal[False]
    error: Literal["permission_denied"]
    reason: str = ""


class AcquireSchemaInvalid(BaseModel):
    ok: Literal[False]
    error: Literal["schema_invalid"]
    detail: Any = None


class AcquireServiceUnavailable(BaseModel):
    ok: Literal[False]
    error: Literal["service_unavailable"]
    # Phase B (Wave 2 boundary hardening): the client may surface a reason
    # here when an unknown error discriminant from the BEAM gets coerced
    # to service_unavailable — the human-readable string names the actual
    # discriminant so registry-drift becomes visible instead of swallowed.
    # None on the legitimate service-unavailable path (advisory escape
    # valve, transport failure, server 503 with no reason).
    reason: str | None = None


AcquireResult: TypeAlias = (
    AcquireOk
    | AcquireHeldByOther
    | AcquirePermissionDenied
    | AcquireSchemaInvalid
    | AcquireServiceUnavailable
)


class StatusOk(BaseModel):
    ok: Literal[True]
    lease: LeaseRecord | None


class StatusSchemaInvalid(BaseModel):
    ok: Literal[False]
    error: Literal["schema_invalid"]
    detail: Any = None


class StatusServiceUnavailable(BaseModel):
    ok: Literal[False]
    error: Literal["service_unavailable"]
    # Phase B (Wave 2 boundary hardening): same semantics as
    # AcquireServiceUnavailable.reason — surfaces the unknown discriminant
    # name when registry drift coerces a BEAM error to service_unavailable.
    reason: str | None = None


StatusResult: TypeAlias = StatusOk | StatusSchemaInvalid | StatusServiceUnavailable


class SimpleOk(BaseModel):
    ok: Literal[True]
    handoff_id: UUID | None = None


class SimpleError(BaseModel):
    ok: Literal[False]
    error: Literal[
        "not_found",
        "expired",
        "not_holder",
        "already_released",
        "permission_denied",
        "schema_invalid",
        "service_unavailable",
    ]
    reason: str | None = None
    detail: Any = None


SimpleResult: TypeAlias = SimpleOk | SimpleError


# ----------------------------------------------------------------------------
# /v1/health — Wave 2 Phase C (supervised health)
# ----------------------------------------------------------------------------

class HealthOk(BaseModel):
    """Successful liveness probe response from BEAM lease-plane /v1/health.

    Minimal envelope by design — Phase C lays down the contract surface;
    future phases extend additively (e.g., `db_ready`, `inflight_leases`).
    Clients tolerate unknown fields per Pydantic default `extra="ignore"`.
    """

    ok: Literal[True]
    status: Literal["ok"]


class HealthUnavailable(BaseModel):
    """Boundary-hardening health probe failure envelope.

    Distinct from a generic `service_unavailable` error: this fires when
    the Python client COULD NOT REACH the BEAM at all (network failure,
    auth misconfiguration, transport timeout) OR when the BEAM responded
    but the body shape didn't validate. Either way, the boundary is not
    confirmed live for monitoring purposes.

    `reason` is human-readable, never None — operators looking at the
    typed result must always see an actionable string.
    """

    ok: Literal[False]
    error: Literal["service_unavailable"]
    reason: str


HealthResult: TypeAlias = HealthOk | HealthUnavailable
