"""Task-local ownership state for the dialectic stuck-session resolver.

The resolver can be entered by the periodic background task or lazily from an
active-session check.  Reviewer selection calls that same active-session check
once per candidate, so the ownership flag has to be shared by both modules:
putting it only in ``reviewer.py`` leaves direct/background resolver calls
unmarked and permits a nested sweep for every candidate.

This ContextVar prevents re-entrancy within one asyncio task tree.  It is not a
database lock and does not serialize independent Python or BEAM processes.
"""

from contextvars import ContextVar


AUTO_RESOLVE_IN_PROGRESS: ContextVar[bool] = ContextVar(
    "dialectic_auto_resolve_in_progress", default=False
)
