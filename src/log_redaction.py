"""Scrub credentials out of uvicorn's request-line log records.

uvicorn logs every WebSocket handshake as ``'%s - "WebSocket %s" [accepted]'``
on the ``uvicorn.error`` logger (not ``uvicorn.access``, so ``access_log=False``
does not silence it), with the full path including the query string. While the
dashboard sent its bearer as ``/ws/eisv?token=…``, every connect wrote the live
``UNITARES_HTTP_API_TOKEN`` into ``mcp_server_error.log``.

The WebSocket no longer reads a query-string token (see
``src.http_routes.access._check_ws_auth``), but a stale browser tab or any other
client can still put one in a URL, and uvicorn would log it all the same. This
filter makes the log safe regardless of what a caller sends.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"

# Query parameters whose values are credentials: any name ending in token,
# key, secret, password or auth (``token``, ``operator_token``, ``api_key``,
# ``access_token``...). Matched case-insensitively after ``?`` or ``&``; the
# value runs to the next ``&``, whitespace, or quote. Over-redacting a harmless
# ``sort_key`` costs nothing; missing a credential is the failure.
_SECRET_PARAM = re.compile(
    r"(?i)([?&][\w.\-]*(?:token|key|secret|password|passwd|auth)=)[^&\s\"']+"
)

UVICORN_LOGGERS = ("uvicorn.error", "uvicorn.access")


def redact(text: str) -> str:
    return _SECRET_PARAM.sub(lambda m: m.group(1) + REDACTED, text)


def _redact_arg(arg):
    return redact(arg) if isinstance(arg, str) else arg


class QuerySecretRedactionFilter(logging.Filter):
    """Rewrite credential query parameters in a record's message and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        return True


def install_uvicorn_redaction() -> None:
    """Attach the filter to uvicorn's loggers. Idempotent.

    Call after ``uvicorn.Config(...)`` is built: Config applies uvicorn's
    logging dictConfig, and attaching afterwards keeps the filter independent
    of whatever that config does to the loggers.
    """
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, QuerySecretRedactionFilter) for f in lg.filters):
            lg.addFilter(QuerySecretRedactionFilter())
