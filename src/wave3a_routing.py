"""Wave 3a per-tool routing table — PR #3 of v0.2 sequencing.

Spec: ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §3.1
("Cutover and rollback shape") and §5 PR #3 ("Python transport per-tool
routing table and rollback script").

This module is **transport-only**. It does not import any handler module
and it does not modify ``TOOL_HANDLERS``. The table sits between the MCP
transport wrapper (``src/mcp_server.py::get_tool_wrapper``) and
``dispatch_tool``. For every dispatch:

- If ``tool_name`` is NOT in the table → the existing Python in-process
  dispatch fires, unchanged. This is the hot path for ~100 tools NOT in
  Wave 3a scope and MUST stay O(1) cheap (a single dict lookup).
- If ``tool_name`` IS in the table → the wrapper consults the BEAM proxy.
  On BEAM success the Python implementation MUST NOT be touched. On BEAM
  failure/timeout/envelope-invalid the wrapper MUST fall back to Python
  (see ``src/wave3a_beam_proxy.py``).

Thread safety: the table is mutated by the rollback admin endpoint
(``src/mcp_handlers/wave3a_admin.py``) and by cutover commands. Reads
happen on every tool dispatch. The whole surface uses a ``threading.RLock``
so concurrent add/remove from multiple async tasks (or admin requests) is
safe; reads acquire the lock too — the lock is uncontended in steady state
because all known mutators are admin operations.

Invariant: empty table at process startup. No row is added without an
explicit cutover (operator action or test fixture). On master, the table
is empty and the proxy code path is never exercised in production.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-handler env-flag cutover hooks (RFC §5 PR #5+)
# ---------------------------------------------------------------------------
#
# Each Wave 3a handler PR adds a single row to ``_ENV_FLAG_ROUTES``: the
# env var the operator flips, the tool name, and the BEAM URL to route to
# when the flag is true. ``apply_env_flag_routes()`` reads the table at MCP
# startup (called from ``src/mcp_server.py`` after the routing table is
# constructed) and adds routes for every enabled flag.
#
# Operator workflow (RFC §3.1 cutover shape):
#
#   1. Land the handler PR. Behavior is unchanged — flag default is OFF.
#   2. Confirm the BEAM listener is healthy (PR #4 launchd plist loaded).
#   3. Run the pre-cutover script (verifies ``requires_identity`` posture).
#   4. Set the env flag in ``~/.config/cirwel/secrets.env``.
#   5. Restart the MCP. ``apply_env_flag_routes`` runs once at boot and
#      adds the route to the table. Subsequent calls flow through BEAM.
#   6. Rollback: clear the env flag and restart, OR call
#      ``scripts/ops/wave-3a-rollback.sh --tool <name>`` (no restart needed).

_ENV_FLAG_ROUTES: Dict[str, Dict[str, str]] = {
    # RFC §5 PR #5 — first cutover.
    "WAVE_3A_HEALTH_CHECK_ON_BEAM": {
        "tool_name": "health_check",
        "beam_url": "http://127.0.0.1:8770/v1/handlers/health_check",
    },
    # PR #6/#7/#8 add their rows here. Each row independent so the operator
    # can cut over handlers one at a time and roll them back independently.
}


def _flag_is_truthy(value: Optional[str]) -> bool:
    """Match the env-flag semantics used elsewhere in the codebase.

    ``true``, ``True``, ``1``, ``yes`` are truthy. Everything else
    (including empty string, ``false``, ``0``, ``no``, missing) is falsy.
    """
    if not value:
        return False
    return value.strip().lower() in {"true", "1", "yes", "y", "on"}


def apply_env_flag_routes() -> List[str]:
    """Read ``_ENV_FLAG_ROUTES`` and apply rows whose env flag is truthy.

    Returns the list of tool names that were added to the routing table.
    Called once at MCP startup from ``src/mcp_server.py`` after the
    routing-table admin surface is wired. Idempotent — a tool already
    present in the table is overwritten with the same URL; the operator
    can re-run the startup hook (e.g., via a launchd one-shot) safely.

    Hard invariant per RFC §3.1: a process restart with the env flag UNSET
    yields an empty routing table. No persistence across restarts.
    """
    added: List[str] = []
    for env_var, row in _ENV_FLAG_ROUTES.items():
        if _flag_is_truthy(os.environ.get(env_var)):
            tool_name = row["tool_name"]
            beam_url = row["beam_url"]
            try:
                set_route(tool_name, beam_url)
                added.append(tool_name)
                logger.info(
                    "[wave3a-routing] env-flag cutover: %s=true → routed %s "
                    "to %s",
                    env_var,
                    tool_name,
                    beam_url,
                )
            except ValueError as exc:
                # Bad config in the env-flag table itself — should never
                # happen in master, but log it explicitly rather than
                # silently dropping the cutover.
                logger.error(
                    "[wave3a-routing] env-flag cutover FAILED for %s: %s",
                    env_var,
                    exc,
                )
    return added


# Module-level state. The table is a single dict; rows added at runtime
# survive only the process lifetime (no persistence). Restart = empty table.
_LOCK = threading.RLock()
_ROUTES: Dict[str, str] = {}


def get_route(tool_name: str) -> Optional[str]:
    """Return the BEAM URL for ``tool_name``, or None if not routed.

    O(1) dict lookup under a fast lock. This is on the hot path for every
    MCP tool call; if anything in this function becomes expensive the
    Wave-3a routing-table change has regressed the entire MCP surface,
    not just the Wave 3a handlers.
    """
    with _LOCK:
        return _ROUTES.get(tool_name)


def set_route(tool_name: str, beam_url: str) -> None:
    """Add or update a routing-table row.

    ``beam_url`` is the full URL on the BEAM listener that will accept the
    tool call. Validated as a non-empty string starting with ``http://``
    or ``https://`` — strict-enough to catch operator typos without
    importing a URL parser.
    """
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError(f"tool_name must be a non-empty string, got {tool_name!r}")
    if not isinstance(beam_url, str) or not (
        beam_url.startswith("http://") or beam_url.startswith("https://")
    ):
        raise ValueError(
            f"beam_url must start with http:// or https://, got {beam_url!r}"
        )
    with _LOCK:
        prev = _ROUTES.get(tool_name)
        _ROUTES[tool_name] = beam_url
    if prev is None:
        logger.info("[wave3a-routing] route added: %s -> %s", tool_name, beam_url)
    else:
        logger.info(
            "[wave3a-routing] route updated: %s %s -> %s", tool_name, prev, beam_url
        )


def remove_route(tool_name: str) -> bool:
    """Drop a single tool from the routing table.

    Returns True if a row was removed, False if the tool was not routed.
    """
    with _LOCK:
        prev = _ROUTES.pop(tool_name, None)
    if prev is not None:
        logger.info("[wave3a-routing] route removed: %s (was %s)", tool_name, prev)
        return True
    return False


def clear_routes() -> int:
    """Drop every row from the routing table.

    Returns the number of rows removed. Used by ``--all`` mode of
    ``scripts/ops/wave-3a-rollback.sh``. Smoke test: calling this on an
    empty table returns 0 and does not raise.
    """
    with _LOCK:
        count = len(_ROUTES)
        _ROUTES.clear()
    if count:
        logger.info("[wave3a-routing] cleared %d route(s)", count)
    return count


def list_routes() -> Dict[str, str]:
    """Return a snapshot of the current routing table.

    Returns a copy — callers cannot mutate the live table via this handle.
    """
    with _LOCK:
        return dict(_ROUTES)


def route_count() -> int:
    """Return the number of rows currently in the routing table."""
    with _LOCK:
        return len(_ROUTES)


def is_routed(tool_name: str) -> bool:
    """Convenience predicate for the dispatch hot path.

    Equivalent to ``get_route(tool_name) is not None`` but explicit about
    the boolean intent at the call site.
    """
    with _LOCK:
        return tool_name in _ROUTES


__all__ = [
    "apply_env_flag_routes",
    "clear_routes",
    "get_route",
    "is_routed",
    "list_routes",
    "remove_route",
    "route_count",
    "set_route",
]
