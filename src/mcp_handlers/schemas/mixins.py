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
        description="Session continuity token from identity(). Include in all calls to maintain identity."
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="UNIQUE agent identifier. Optional if session-bound (auto-injected)."
    )
