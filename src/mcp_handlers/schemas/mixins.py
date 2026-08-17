from typing import Optional
from pydantic import BaseModel, Field

class AgentIdentityMixin(BaseModel):
    """Common parameters for tools that require agent orchestration."""
    continuity_token: Optional[str] = Field(
        default=None,
        description=(
            "Signed ownership proof from onboard()/identity(). Use only for "
            "same-live-process PATH 0 rebinds with agent_uuid; do not use as "
            "a cross-process resume credential."
        )
    )
    client_session_id: Optional[str] = Field(
        default=None,
        description=(
            "In-session binding identifier from start_session()/identity(). "
            "Pass it on same-process calls when the adapter does not inject it "
            "automatically; it is not a continuity_token or cross-process proof."
        )
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="UNIQUE agent identifier. Optional if session-bound (auto-injected)."
    )
