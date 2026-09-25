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
        # These ride on nearly every tool, so their advertised length is paid
        # for once per tool per tools/list: continuity_token and
        # client_session_id on all 13 progressive tools, agent_id on 11. The
        # authored short forms keep the part that prevents misuse and leave
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
            # Not "from start_session": on start_session itself it can be an
            # orchestrator-anchored input.
            "brief": "Binding id for calls in this process; not a cross-process proof."
        },
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="UNIQUE agent identifier. Optional if session-bound (auto-injected).",
        json_schema_extra={
            # "UUID" is the wire word that separates this input from the
            # cosmetic display handle a response carries (#1537, 8a3b63f8: a
            # label passed here collided in find_agent_by_label).
            "brief": "UUID; leave unset for yourself."
        },
    )
