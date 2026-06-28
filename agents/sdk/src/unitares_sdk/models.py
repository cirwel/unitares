"""Pydantic response models for governance tool results."""

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class _GovModel(BaseModel):
    """Base model with extra="ignore" so unknown server fields don't break parsing."""

    model_config = ConfigDict(extra="ignore")


class OnboardResult(_GovModel):
    success: bool
    client_session_id: str
    uuid: str | None = None
    continuity_token: str | None = None
    continuity_token_supported: bool = False
    is_new: bool = False
    verdict: str = "proceed"
    guidance: str | None = None
    session_resolution_source: str | None = None
    welcome: str | None = None


class IdentityResult(_GovModel):
    client_session_id: str
    uuid: str
    continuity_token: str | None = None
    resolution_source: str | None = Field(
        default=None,
        validation_alias=AliasChoices("resolution_source", "session_resolution_source"),
    )


class CheckinResult(_GovModel):
    success: bool
    verdict: str  # proceed/guide/pause/reject
    guidance: str | None = None
    margin: str | None = None
    coherence: float | None = None
    risk: float | None = None
    metrics: dict | None = None


class NoteResult(_GovModel):
    success: bool
    discovery_id: str | None = None


class SearchResult(_GovModel):
    success: bool = True
    error: str | None = None
    results: list[dict] = Field(default_factory=list)


class AuditResult(_GovModel):
    success: bool
    results: list[dict] = Field(default_factory=list)
    # Server returns audit data under `audit` at the top level, not `results`.
    # Kept `results` above for backwards compatibility; new consumers should
    # read buckets/top_stale from `audit`.
    audit: dict | None = None
    error: str | None = None


class CleanupResult(_GovModel):
    success: bool
    cleaned: int = 0
    # Server wraps detail under `cleanup_result`; `cleaned` stays 0 without
    # this field because the wire shape uses different counter names
    # (discoveries_archived, ephemeral_archived, discoveries_deleted).
    cleanup_result: dict | None = None

    @property
    def cleaned_total(self) -> int:
        """Sum of all archive/delete counters from the server's cleanup_result."""
        if not isinstance(self.cleanup_result, dict):
            return self.cleaned
        return (
            self.cleanup_result.get("discoveries_archived", 0)
            + self.cleanup_result.get("ephemeral_archived", 0)
            + self.cleanup_result.get("discoveries_deleted", 0)
        )


class ArchiveResult(_GovModel):
    success: bool
    archived: int = 0
    # Server returns `archived_count`, not `archived`. Keep both so older
    # callers don't break while new code reads archived_count directly.
    archived_count: int = 0
    would_archive_count: int = 0
    would_archive_agents: list[dict] = Field(default_factory=list)
    dry_run: bool = False


class RecoveryResult(_GovModel):
    success: bool
    action_taken: str | None = None


class MetricsResult(_GovModel):
    success: bool
    metrics: dict = Field(default_factory=dict)


class ModelResult(_GovModel):
    success: bool
    response: str | None = None
