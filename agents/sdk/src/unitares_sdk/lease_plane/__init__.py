"""Python contract for the UNITARES lease plane.

The Elixir/OTP node owns live coordination, but Python callers use this
package as the stable boundary. Raw HTTP calls should stay behind this module.
"""

# Wave 2 §"Lease-integration boundary hardening" — versioned contracts.
# Bumped when the lease-plane response shapes change in a way that requires
# a coordinated client/server deploy. Mirrored on the Elixir side at
# `elixir/lease_plane/lib/unitares_lease_plane/http_router.ex` —
# `@protocol_version`. Kept in sync by:
#   - `tests/test_lease_plane_protocol_version.py` on the Python side
#   - `test/unitares_lease_plane/http_router_protocol_version_test.exs` on the
#     Elixir side (each pinning the literal "v1.0")
# Mismatch behavior: Python client logs WARNING, does NOT fail (rollout
# grace). See `LeasePlaneClient._request_json`.
PROTOCOL_VERSION = "v1.0"

from .client import LeasePlaneClient, LeasePlaneClientConfig, LeasePlaneDisabledClient
from .models import (
    AcquireHeldByOther,
    AcquireOk,
    AcquirePermissionDenied,
    AcquireRequest,
    AcquireResult,
    AcquireSchemaInvalid,
    AcquireServiceUnavailable,
    EarnedStatus,
    ForceReleaseRequest,
    HandoffAcceptRequest,
    HandoffOfferRequest,
    HealthOk,
    HealthResult,
    HealthUnavailable,
    HeartbeatRequest,
    LeaseRecord,
    ReleaseReason,
    ReleaseRequest,
    RenewRequest,
    SimpleError,
    SimpleOk,
    SimpleResult,
    StatusOk,
    StatusResult,
    StatusSchemaInvalid,
    StatusServiceUnavailable,
)

__all__ = [
    "PROTOCOL_VERSION",
    "AcquireHeldByOther",
    "AcquireOk",
    "AcquirePermissionDenied",
    "AcquireRequest",
    "AcquireResult",
    "AcquireSchemaInvalid",
    "AcquireServiceUnavailable",
    "EarnedStatus",
    "ForceReleaseRequest",
    "HandoffAcceptRequest",
    "HandoffOfferRequest",
    "HealthOk",
    "HealthResult",
    "HealthUnavailable",
    "HeartbeatRequest",
    "LeasePlaneClient",
    "LeasePlaneClientConfig",
    "LeasePlaneDisabledClient",
    "LeaseRecord",
    "ReleaseReason",
    "ReleaseRequest",
    "RenewRequest",
    "SimpleError",
    "SimpleOk",
    "SimpleResult",
    "StatusOk",
    "StatusResult",
    "StatusSchemaInvalid",
    "StatusServiceUnavailable",
]
