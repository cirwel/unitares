"""Resolve the HTTP client library the installed ``mcp`` transport expects.

mcp 2.x rewrote ``mcp.client.streamable_http`` against ``httpx2``: it calls
``client.sse(...)`` on whatever is handed to ``http_client=`` and catches
``httpx2.StreamError``. An ``httpx`` 0.x client has no ``.sse()``, and the
AttributeError is swallowed by the transport's retry loop — POST round-trips
keep working while the server-push GET stream dies SILENTLY. A connect test
that only round-trips requests passes against a client that can never receive
a server notification.

Because the failure is silent, this resolves the seam by reading the transport
module's own import surface rather than branching on the ``mcp`` version: the
question is "what library does this transport call methods on", and asking it
directly stays correct if a future ``mcp`` switches back or switches again.

This duplicates ``src/mcp_compat.mcp_httpx`` in the server repo on purpose —
``unitares-sdk`` ships as its own distribution and must not import from the
server tree. Keep the two in sync; ``tests/test_mcp_dependency_canary.py``
covers the seam for both.
"""

from __future__ import annotations

from typing import Any

_MCP_HTTPX: Any = None


def mcp_httpx() -> Any:
    """Return the HTTP client library the installed ``mcp`` transport expects.

    Use it for clients injected via ``http_client=`` and for the exception
    types those clients raise::

        httpx = mcp_httpx()
        client = httpx.AsyncClient(http2=False, timeout=30)
        except httpx.ConnectError: ...

    Cached — the answer cannot change within a process.

    Raises:
        ImportError: the transport exposes neither ``httpx2`` nor ``httpx``
            under its own name, so its expectation cannot be read off it.
            Guessing would re-open the silent-failure path above, so this
            fails loudly instead.
    """
    global _MCP_HTTPX
    if _MCP_HTTPX is None:
        _MCP_HTTPX = _resolve()
    return _MCP_HTTPX


def _resolve() -> Any:
    import mcp.client.streamable_http as transport

    for name in ("httpx2", "httpx"):
        mod = getattr(transport, name, None)
        # Identity, not just presence: ``import httpx2 as httpx`` would satisfy
        # a bare getattr while being the other library.
        if mod is not None and getattr(mod, "__name__", None) == name:
            return mod

    raise ImportError(
        "Cannot determine which HTTP client library mcp.client.streamable_http "
        "is written against: it exposes neither 'httpx2' nor 'httpx' at module "
        "level under its own name. Injecting the wrong one kills the "
        "server-push GET stream silently, so this refuses to guess. Re-base "
        "this resolver and the import-surface check in "
        "tests/test_mcp_dependency_canary.py against the new transport."
    )
