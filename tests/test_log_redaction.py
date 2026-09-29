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


# ---- Percent-encoded parameter names (Codex review finding, PR #2577) ----

class TestPercentEncodedParamNames:
    """A caller can percent-encode the parameter name to bypass the regex.

    ``to%6ben`` decodes to ``token`` via URLSearchParams but the raw query
    string carries the encoded form and the regex misses it.  The fix decodes
    only the *name* portion before matching, leaving value bytes intact.
    """

    def test_hex_encoded_letter_in_name(self):
        # to%6ben -> token (%6b == 'k')
        assert SECRET not in redact(f"/ws?to%6ben={SECRET}")
        assert REDACTED in redact(f"/ws?to%6ben={SECRET}")

    def test_underscore_encoded_in_compound_name(self):
        # operator%5ftoken -> operator_token (%5f == '_')
        assert SECRET not in redact(f"/ws?operator%5ftoken={SECRET}")

    def test_uppercase_hex_encoding(self):
        # to%4BEN -> toKEN (%4B == 'K', case-insensitive suffix match)
        assert SECRET not in redact(f"/ws?to%4BEN={SECRET}")

    def test_fully_encoded_name(self):
        # %74%6f%6b%65%6e -> token
        assert SECRET not in redact(f"/ws?%74%6f%6b%65%6e={SECRET}")

    def test_non_credential_encoded_name_not_redacted(self):
        # %73ort -> sort (not a credential suffix)
        url = "/search?%73ort=desc"
        assert REDACTED not in redact(url)

    def test_filter_path_also_catches_encoded_name(self):
        out = _format('%s - "WebSocket %s" [accepted]', "1.2.3.4", f"/ws?to%6ben={SECRET}")
        assert SECRET not in out
        assert REDACTED in out
