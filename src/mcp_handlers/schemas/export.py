from typing import ClassVar, Literal, Mapping, Optional, Tuple
from pydantic import Field
from .mixins import AgentIdentityMixin

class GetSystemHistoryParams(AgentIdentityMixin):
    """
    Export complete governance history for an agent.
    """
    format: Literal["json", "csv"] = Field(
        default="json",
        description="Output format."
    )

class ExportToFileParams(AgentIdentityMixin):
    """
    Export governance history to a file in the server's data directory.
    """
    format: Literal["json", "csv"] = Field(
        default="json",
        description="Output format (json or csv)"
    )
    filename: Optional[str] = Field(
        default=None,
        description="Optional custom filename (without extension). If not provided, uses agent_id with timestamp."
    )
    complete_package: bool = Field(
        default=False,
        description=(
            "If true, exports metadata, the record (checkpoints, findings the "
            "agent authored and outcomes, newest 1000 of each), the EISV history "
            "when readable, and validation. If false (default), exports history only."
        )
    )

class ExportParams(AgentIdentityMixin):
    """Unified governance history export operations."""
    # Which of these flat parameters each action uses. The wire schema stays
    # flat (the MCP wrapper builds its argument model from top-level
    # properties), so this is the only machine-readable statement of the
    # per-action contract; describe_tool(action=...) serves it and
    # tests/test_router_action_fields.py holds it to the routing table.
    # Identity and session parameters are common to every action and are
    # not repeated here (schemas/router_actions.COMMON_ROUTER_FIELDS).
    ACTION_FIELDS: ClassVar[Mapping[str, Tuple[str, ...]]] = {
        "history": (
                "format",
        ),
        "file": (
                "format", "filename", "complete_package",
        ),
    }
    action: Literal["history", "file"] = Field(
        "history",
        description="Operation to perform",
    )
    format: Literal["json", "csv"] = Field(
        "json",
        description="Output format",
    )
    filename: Optional[str] = Field(
        None,
        description="Custom filename without an extension for action=file",
    )
    complete_package: bool = Field(
        False,
        description=(
            "Export metadata, the record (checkpoints, authored findings, outcomes), "
            "the EISV history and validation together for action=file"
        ),
    )
