"""UNITARES Agent SDK — typed client and lifecycle base class for governance agents."""

from unitares_sdk.errors import (
    GovernanceConnectionError,
    GovernanceError,
    GovernanceTimeoutError,
    IdentityDriftError,
    VerdictError,
)
from unitares_sdk.models import (
    ArchiveResult,
    AuditResult,
    CheckinResult,
    CleanupResult,
    IdentityResult,
    InferenceHost,
    InferenceHostResult,
    InferenceHostsResult,
    InferenceProvenance,
    MetricsResult,
    ModelResult,
    NoteResult,
    OnboardResult,
    RecoveryResult,
    SearchResult,
)

__all__ = [
    # Clients (imported lazily by consumers)
    "GovernanceClient",
    "SyncGovernanceClient",
    # Agent base class
    "GovernanceAgent",
    "CycleResult",
    # Models
    "ArchiveResult",
    "AuditResult",
    "CheckinResult",
    "CleanupResult",
    "IdentityResult",
    "InferenceHost",
    "InferenceHostResult",
    "InferenceHostsResult",
    "InferenceProvenance",
    "MetricsResult",
    "ModelResult",
    "NoteResult",
    "OnboardResult",
    "RecoveryResult",
    "SearchResult",
    # Errors
    "GovernanceError",
    "GovernanceConnectionError",
    "GovernanceTimeoutError",
    "IdentityDriftError",
    "VerdictError",
]


def __getattr__(name: str):
    """Lazy imports for heavier modules to keep initial import fast."""
    if name == "GovernanceClient":
        from unitares_sdk.client import GovernanceClient

        return GovernanceClient
    if name == "SyncGovernanceClient":
        from unitares_sdk.sync_client import SyncGovernanceClient

        return SyncGovernanceClient
    if name == "GovernanceAgent":
        from unitares_sdk.agent import GovernanceAgent

        return GovernanceAgent
    if name == "CycleResult":
        from unitares_sdk.agent import CycleResult

        return CycleResult
    raise AttributeError(f"module 'unitares_sdk' has no attribute {name!r}")
