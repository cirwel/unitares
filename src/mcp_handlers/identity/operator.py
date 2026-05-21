"""
Operator-tier authorization for handlers that need to disclose
cross-agent UUIDs (initially: list_agents).

Background — KG 2026-04-20T00:57:45 found that ``list_agents`` returns
every agent's UUID to any caller, enabling a two-call identity hijack
when combined with the PATH 1 ``agent-{uuid12}`` prefix-bind. The
proposed fix at ````
redacts UUIDs except for "operator-class" callers — trusted
infrastructure (Discord bridge, dashboard, ollama bridge) that
genuinely needs full visibility.

Code review of that proposal showed that ``client_session_id`` cannot
serve as the operator credential: it is a transport fingerprint
(IP:UA, MCP session header, or ``agent-{uuid12}`` prefix), not an
application-level bearer token. This module is the explicit auth
surface that the redaction PR will read from.

Design:

- Operators present an ``X-Unitares-Operator: <token>`` header.
- The header is captured into ``SessionSignals.unitares_operator_token``
  by the ASGI/HTTP layer.
- Tokens are compared against ``UNITARES_OPERATOR_TOKENS`` (csv env
  var). Empty/unset → operator status is unavailable to anyone.
- Default deny: if no token is presented or no allowlist is
  configured, ``is_operator_caller`` returns False.

Token storage:

- Tokens are deployment secrets. Recommended location:
  ``~/.config/cirwel/secrets.env`` (mode 600) per
  ``project_secrets-location`` memory.
- Bridge/dashboard/ollama plists or systemd units load the token
  into the client process and pass it on every request.
- Rotation is operator action: update the env var in
  ``UNITARES_OPERATOR_TOKENS`` and reissue tokens to clients.
- Tokens should be high-entropy random strings (e.g. ``openssl rand
  -hex 32``). Use one token per client identity, not a shared one,
  so a compromise can be revoked without disrupting all operators.
"""

import os
from typing import Optional, Set

from src.mcp_handlers.context import SessionSignals, get_session_signals


_OPERATOR_TOKENS_ENV = "UNITARES_OPERATOR_TOKENS"


def _allowlisted_tokens() -> Set[str]:
    """Parse the operator-token allowlist from env at call time.

    Read fresh each call rather than caching, so operators can rotate
    tokens without restarting the server. The set is small (a handful
    of entries) and the env read is cheap.
    """
    raw = os.environ.get(_OPERATOR_TOKENS_ENV, "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def is_operator_caller(signals: Optional[SessionSignals] = None) -> bool:
    """Return True if the current request presents a valid operator token.

    The check is two-part:

    1. The request must carry a non-empty ``X-Unitares-Operator``
       header, captured into ``SessionSignals.unitares_operator_token``.
    2. The presented value must match a token in
       ``UNITARES_OPERATOR_TOKENS`` (csv env var).

    Both halves must hold. Empty header, missing env var, or empty
    allowlist all yield False (default deny).

    ``signals`` is optional; if omitted, we read from the contextvar
    set by the ASGI/HTTP layer. Callers in non-request contexts (e.g.
    in-process resident agents) will see ``signals=None`` and get
    False — they have other paths to operator-class data and should
    not pretend to be HTTP operators.
    """
    if signals is None:
        signals = get_session_signals()
    if signals is None:
        return False

    presented = signals.unitares_operator_token
    if not presented:
        return False

    allowlist = _allowlisted_tokens()
    if not allowlist:
        return False

    return presented in allowlist
