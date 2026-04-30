"""Typed request and response models for the lease-plane HTTP contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    @model_validator(mode="after")
    def _holder_kind_matches_heartbeat(self) -> "LeaseRecord":
        expected = self.holder_kind == "remote_heartbeat"
        if self.heartbeat_required is not expected:
            raise ValueError("heartbeat_required must match holder_kind")
        return self


class AcquireRequest(LeasePlaneModel):
    """Request body for POST /v1/lease/acquire."""

    surface_id: str = Field(min_length=1)
    surface_kind: str = Field(min_length=1)
    holder_agent_uuid: UUID
    holder_class: HolderClass
    holder_kind: HolderKind
    ttl_s: int = Field(gt=0, le=3600)
    holder_pid: str | None = None
    intent: str | None = None
    audit_session: str | None = None


class RenewRequest(LeasePlaneModel):
    """Request body for POST /v1/lease/renew.

    The renew contract intentionally accepts no ttl_s. The lease plane extends
    by the immutable original_ttl_s stored at acquire time.
    """

    lease_id: UUID


class HeartbeatRequest(RenewRequest):
    """Request body for POST /v1/lease/heartbeat."""


class ReleaseRequest(LeasePlaneModel):
    """Request body for POST /v1/lease/release."""

    lease_id: UUID
    release_reason: ReleaseReason = "normal"


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
    held_by_uuid: UUID
    expires_at: datetime


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
