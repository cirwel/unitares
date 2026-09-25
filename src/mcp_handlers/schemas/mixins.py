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
        ),
        # These three ride on nearly every tool, so their advertised length is
        # paid for once per tool per tools/list (13x on the progressive
        # surface). The authored short forms keep the part that prevents
        # misuse — what each is NOT — in as few words as carry it, and leave
        # the rest to describe_tool.
        json_schema_extra={
            "brief": "Same-process rebind proof only; never a cross-process resume."
        },
    )
    client_session_id: Optional[str] = Field(
        default=None,
        description=(
            "In-session binding identifier from start_session()/identity(). "
            "Pass it on same-process calls when the adapter does not inject it "
            "automatically; it is not a continuity_token or cross-process proof."
        ),
        json_schema_extra={
            "brief": "From start_session; pass on calls in this process only."
        },
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="UNIQUE agent identifier. Optional if session-bound (auto-injected).",
        json_schema_extra={
            "brief": "Agent id; optional when session-bound."
        },
    )
