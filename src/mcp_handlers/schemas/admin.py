from typing import Optional, Union, Literal, Dict, Any
from pydantic import Field, model_validator
from .mixins import AgentIdentityMixin

class ListToolsParams(AgentIdentityMixin):
    """
    List all available governance tools with descriptions and categories.
    """
    essential_only: Union[bool, str, None] = Field(
        default=False,
        description="If true, return only Tier 1 (essential) tools."
    )
    verbose: Union[bool, str, None] = Field(
        default=False,
        description="If true, include full schema parameters."
    )
    category: Optional[str] = Field(
        default=None,
        description="Filter tools by category."
    )
    progressive: Union[bool, str, None] = Field(
        default=False,
        description="If true, order tools by usage frequency."
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        def _to_bool(val: Any) -> bool:
            if isinstance(val, str):
                return val.lower() in ('true', '1', 'yes')
            return bool(val)

        if self.essential_only is not None:
            self.essential_only = _to_bool(self.essential_only)
        if self.verbose is not None:
            self.verbose = _to_bool(self.verbose)
        if self.progressive is not None:
            self.progressive = _to_bool(self.progressive)
        
        return self

class DescribeToolParams(AgentIdentityMixin):
    """
    Return full details for a single tool.
    """
    tool_name: str = Field(..., description="Exact name of the tool to describe.")
    lite: Union[bool, str, None] = Field(
        default=False,
        description="If true, return simplified schema with examples."
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.lite, str):
            self.lite = self.lite.lower() in ('true', '1', 'yes')
        return self

class UpdateConfigParams(AgentIdentityMixin):
    """
    Update server configuration safely.
    """
    target: str = Field(..., description="Configuration group/section.")
    parameter: str = Field(..., description="Specific parameter to update.")
    value: Any = Field(..., description="New value for the parameter.")
    dry_run: Union[bool, str, None] = Field(
        default=True,
        description="If true, validates without applying."
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.dry_run, str):
            self.dry_run = self.dry_run.lower() in ('true', '1', 'yes')
        return self

class GetServerInfoParams(AgentIdentityMixin):
    """
    Report internal diagnostics for the UNITARES system.
    """
    detail: Literal["basic", "full"] = Field(
        default="basic",
        description="Detail level."
    )

class HealthCheckParams(AgentIdentityMixin):
    """
    Perform deep diagnostic check of the governance system.
    """
    lite: bool = Field(True, description="Lite mode (default: true). Returns only component statuses without nested info/stats blocks. Set to false for full diagnostic detail.")

class GetTelemetryMetricsParams(AgentIdentityMixin):
    """Parameters for get_telemetry_metrics"""
    agent_id: Optional[str] = Field(None, description="Optional agent ID to filter metrics. If not provided, returns metrics for all agents.")
    include_calibration: bool = Field(False, description="Include full calibration metrics (default: false). Calibration data is system-wide and can be large, so it's excluded by default to reduce context bloat. Use check_calibration tool for detailed calibration analysis.")
    window_hours: float = Field(24, description="Time window in hours for metrics (default: 24)")


class GetToolUsageStatsParams(AgentIdentityMixin):
    """Parameters for get_tool_usage_stats"""
    window_hours: float = Field(168, description="Time window in hours for statistics (default: 168 = 7 days)")
    tool_name: Optional[str] = Field(None, description="Optional: Filter by specific tool name")
    agent_id: Optional[str] = Field(None, description="Optional: Filter by specific agent ID")


class GetConnectionStatusParams(AgentIdentityMixin):
    """Parameters for get_connection_status"""
    pass


class ResetMonitorParams(AgentIdentityMixin):
    """Parameters for reset_monitor"""
    agent_id: Optional[str] = Field(None, description="Agent identifier")


class GetThresholdsParams(AgentIdentityMixin):
    """Parameters for get_thresholds"""


class SetThresholdsParams(AgentIdentityMixin):
    """Parameters for set_thresholds"""
    thresholds: Dict[str, Any] = Field(..., description="Dict of threshold_name -> value. Valid keys: risk_approve_threshold, risk_revise_threshold, coherence_critical_threshold, void_threshold_initial")
    validate_params: bool = Field(True, alias="validate", description="Validate values are in reasonable ranges")


class CleanupStaleLocksParams(AgentIdentityMixin):
    """Parameters for cleanup_stale_locks"""
    max_age_seconds: float = Field(300.0, description="Maximum age in seconds before considering stale (default: 300 = 5 minutes)")
    dry_run: bool = Field(False, description="If True, only report what would be cleaned (default: False)")


class GetLifecycleStatsParams(AgentIdentityMixin):
    """Parameters for get_lifecycle_stats"""
    pass


class DebugRequestContextParams(AgentIdentityMixin):
    """Parameters for debug_request_context"""


class ConfigParams(AgentIdentityMixin):
    """Parameters for config"""


class AdminParams(AgentIdentityMixin):
    """Unified admin / diagnostics operations.

    Consolidates the low-traffic operator/diagnostic single-purpose tools
    (server_info, connections, workspace_health, tool_usage, telemetry,
    debug_context, validate_path, reset_monitor, cleanup_locks) behind one
    action router. The original single-purpose tools remain registered for
    backwards compatibility; this router is the discoverable surface.
    """
    action: Literal[
        "server_info",
        "connections",
        "workspace_health",
        "tool_usage",
        "telemetry",
        "debug_context",
        "validate_path",
        "reset_monitor",
        "cleanup_locks",
    ] = Field(..., description="Diagnostic/maintenance operation to perform")
    # server_info
    detail: Literal["basic", "full"] = Field(
        "basic", description="Detail level (for action=server_info)."
    )
    # telemetry / tool_usage — window_hours default diverges by action and is
    # resolved in the validator below (telemetry=24, tool_usage=168) so the
    # underlying handlers keep their original defaults without edits.
    window_hours: Optional[float] = Field(
        None,
        description="Time window in hours (for action=telemetry default 24, action=tool_usage default 168).",
    )
    include_calibration: bool = Field(
        False,
        description="Include full calibration metrics (for action=telemetry). Default false to reduce response size.",
    )
    tool_name: Optional[str] = Field(
        None, description="Filter by a specific tool name (for action=tool_usage)."
    )
    # cleanup_locks
    max_age_seconds: float = Field(
        300.0,
        description="Max age in seconds before a lock is stale (for action=cleanup_locks). Default 300.",
    )
    dry_run: bool = Field(
        False,
        description="If true, report what would be cleaned without acting (for action=cleanup_locks).",
    )
    # validate_path
    file_path: Optional[str] = Field(
        None, description="Path to validate against project policy (required for action=validate_path)."
    )

    @model_validator(mode="after")
    def _apply_action_window_defaults(self):
        # Mirror the per-handler window_hours defaults so omitting the param
        # routes the same value the standalone tool would have used.
        if self.window_hours is None:
            if self.action == "tool_usage":
                self.window_hours = 168.0
            elif self.action == "telemetry":
                self.window_hours = 24.0
        return self


