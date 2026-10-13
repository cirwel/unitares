"""Which identifier a caller may be shown for an agent.

The listing (``agent(action="list")``) withholds the registry UUID from a
caller who is neither the operator nor the agent itself, and prints a public
handle in its place. This module is that rule as one public call, for handlers
outside the lifecycle package that return an agent id. A second surface that
re-derived the rule could drift from the listing and hand back what the
listing withholds; the 2026-10-11 observe fix is the case that prompted it.

The rule itself stays in ``query`` beside the listing that owns it. It is
reached through the module so a test that patches the caller or operator
lookup there still governs this call.

``listed_agent_identifier`` is the other half: the exact string the listing
prints for an agent when it withholds the UUID. A lookup that accepts what the
listing printed has to compare against this and nothing re-derived, or the two
drift and a printed id stops resolving.
"""

from __future__ import annotations

from typing import Any

from . import query as _query


def visible_agent_identifier_for_caller(agent_id: str, meta: Any) -> tuple[str, bool]:
    """The id the current caller may be shown for ``agent_id``, and whether
    the UUID was withheld."""
    return _query._visible_agent_identifier(
        agent_id,
        meta,
        caller_uuid=_query._context_agent_id(),
        operator_caller=_query._is_operator_request(),
    )


def listed_agent_identifier(agent_id: str, meta: Any) -> str:
    """The public identifier the listing prints for ``agent_id`` when it
    withholds the UUID: the disambiguated handle, else the label or display
    name, else the ``agent-redacted-<digest>`` fallback."""
    return _query._public_agent_identifier(agent_id, meta)
