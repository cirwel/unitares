"""Pydantic schema for the `skills` MCP introspection tool.



The tool is read-only and identity-blind (§4.5): identity-derived parameters
on the request payload are accepted (so adapters can pass through MCP
identity middleware without dropping fields) but MUST NOT vary the response.
"""

from typing import Optional

from pydantic import BaseModel, Field


class SkillsParams(BaseModel):
    """Request shape for the `skills` introspection tool."""

    name: Optional[str] = Field(
        default=None,
        description="Return only the skill matching this exact name (e.g. 'governance-lifecycle'). If absent, return the full bundle.",
    )
    since_version: Optional[str] = Field(
        default=None,
        description="ISO date string. Return only skills with last_verified > since_version. Used by client adapters for cheap re-poll cache invalidation.",
    )
