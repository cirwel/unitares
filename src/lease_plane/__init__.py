"""Python contract for the UNITARES lease plane.

The Elixir/OTP node owns live coordination, but Python callers use this
package as the stable boundary. Raw HTTP calls should stay behind this module.
"""

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
