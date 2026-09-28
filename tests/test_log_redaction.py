"""uvicorn's handshake log must never persist a query-string credential.

uvicorn logs each WebSocket handshake on ``uvicorn.error`` with the full path,
query string included. While the dashboard connected to ``/ws/eisv?token=…``
that put the live ``UNITARES_HTTP_API_TOKEN`` into ``mcp_server_error.log`` on
every connect. These tests pin the filter that scrubs it.
"""

from __future__ import annotations

import logging

import pytest

from src.log_redaction import (
    REDACTED,
    UVICORN_LOGGERS,
    QuerySecretRedactionFilter,
    install_uvicorn_redaction,
    redact,
)

SECRET = "0123456789abcdef" * 4


@pytest.fixture(autouse=True)
def _restore_uvicorn_filters():
    saved = {n: list(logging.getLogger(n).filters) for n in UVICORN_LOGGERS}
    yield
    for n, filters in saved.items():
        logging.getLogger(n).filters[:] = filters


def _format(msg, *args):
    rec = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, msg, args, None)
    QuerySecretRedactionFilter().filter(rec)
    return rec.getMessage()


def test_uvicorn_websocket_handshake_line_is_scrubbed():
    # The exact format string uvicorn's websockets implementations use.
    out = _format(
        '%s - "WebSocket %s" [accepted]', "('2600::1', 0)", f"/ws/eisv?token={SECRET}"
    )
    assert SECRET not in out
    assert f"/ws/eisv?token={REDACTED}" in out


def test_other_params_survive_and_every_secret_param_is_scrubbed():
    out = redact(f"/x?since=5&api_key={SECRET}&limit=10&Token={SECRET}")
    assert SECRET not in out
    assert "since=5" in out and "limit=10" in out


def test_prefixed_credential_params_are_scrubbed():
    # The dashboard provisions ?operator_token=… by page URL; the page GET is
    # logged on uvicorn.access like any other request line.
    out = redact(f'"GET /dashboard?operator_token={SECRET}&x=1 HTTP/1.1" 200')
    assert SECRET not in out
    assert "x=1" in out


def test_non_credential_text_is_untouched():
    line = '127.0.0.1:5000 - "GET /v1/residents?limit=5 HTTP/1.1" 200'
    assert redact(line) == line


def test_dict_args_and_non_strings_are_handled():
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        "%(path)s %(code)d",
        ({"path": f"/ws/eisv?token={SECRET}", "code": 200},),
        None,
    )
    QuerySecretRedactionFilter().filter(rec)
    assert SECRET not in rec.getMessage()
    assert rec.getMessage().endswith(" 200")


def test_install_is_idempotent():
    install_uvicorn_redaction()
    install_uvicorn_redaction()
    for name in UVICORN_LOGGERS:
        n = sum(
            isinstance(f, QuerySecretRedactionFilter)
            for f in logging.getLogger(name).filters
        )
        assert n == 1


def test_filter_survives_a_later_uvicorn_config():
    # The public listener builds a second uvicorn.Config after install, which
    # re-applies uvicorn's logging dictConfig. The filter must still be there.
    uvicorn = pytest.importorskip("uvicorn")

    async def app(scope, receive, send):  # pragma: no cover - never served
        pass

    install_uvicorn_redaction()
    uvicorn.Config(app, log_level="info")
    for name in UVICORN_LOGGERS:
        assert any(
            isinstance(f, QuerySecretRedactionFilter)
            for f in logging.getLogger(name).filters
        ), name


def test_transport_installs_the_filter_after_building_config():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src/services/mcp_transport_service.py"
    text = src.read_text()
    assert "install_uvicorn_redaction()" in text
    assert text.index("config = uvicorn.Config(") < text.index(
        "install_uvicorn_redaction()"
    )
