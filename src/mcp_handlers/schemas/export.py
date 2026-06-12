from typing import Optional, Literal
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
        description="If true, exports complete package (metadata + history + knowledge + validation). If false (default), exports history only."
    )

class ExportParams(AgentIdentityMixin):
    """Parameters for export"""


