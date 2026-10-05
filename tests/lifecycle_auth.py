"""Caller standing for lifecycle-handler tests (GHSA-r9q5-7j8h-82rr).

``archive``, ``delete``, ``resume`` and ``operator_resume_agent`` accept two
principals: the target agent itself and a caller presenting a valid
``X-Unitares-Operator`` token. Handler-level tests call the handlers with no
transport underneath, so they have to say which principal they are; these
context managers set exactly the state the real transport would.
"""

from __future__ import annotations

import contextlib
import os
from typing import Iterator, Optional

TEST_OPERATOR_TOKEN = "test-operator-token"


@contextlib.contextmanager
def operator_caller(token: str = TEST_OPERATOR_TOKEN) -> Iterator[None]:
    """The request presents ``token`` and the server allowlists it."""
    from src.mcp_handlers.context import (
        SessionSignals,
        reset_session_signals,
        set_session_signals,
    )

    previous = os.environ.get("UNITARES_OPERATOR_TOKENS")
    os.environ["UNITARES_OPERATOR_TOKENS"] = token
    signals_token = set_session_signals(
        SessionSignals(transport="rest", unitares_operator_token=token)
    )
    try:
        yield
    finally:
        reset_session_signals(signals_token)
        if previous is None:
            os.environ.pop("UNITARES_OPERATOR_TOKENS", None)
        else:
            os.environ["UNITARES_OPERATOR_TOKENS"] = previous


@contextlib.contextmanager
def no_operator() -> Iterator[None]:
    """The request presents no operator token at all."""
    from src.mcp_handlers.context import (
        SessionSignals,
        reset_session_signals,
        set_session_signals,
    )

    signals_token = set_session_signals(SessionSignals(transport="rest"))
    try:
        yield
    finally:
        reset_session_signals(signals_token)


@contextlib.contextmanager
def bound_caller(agent_id: Optional[str]) -> Iterator[None]:
    """The session is bound to ``agent_id`` (``None`` = unbound)."""
    from src.mcp_handlers.context import reset_session_context, set_session_context

    context_token = set_session_context(
        session_key="test-lifecycle-session",
        client_session_id="test-lifecycle-session",
        agent_id=agent_id,
    )
    try:
        yield
    finally:
        reset_session_context(context_token)
