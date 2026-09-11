"""Tests for the doctor's MCP-route gate check.

The check exists because /health and /mcp/ disagree: DNS-rebinding protection
validates the Host on the MCP route only, so a deployment whose allowlist is
missing the public hostname serves 421 on every MCP call while every health
surface reads 200.

Two properties are load-bearing and easy to lose:

* The probe must carry the EXTERNAL Host to the LOCAL listener. Sending the
  loopback Host would pass on exactly the broken deployment; connecting to the
  external host would turn the check into a tunnel test and forfeit the
  "needs no egress" property. Both halves are pinned.
* 401 must never be a PASS. This repo runs its auth gate ahead of the SDK's
  Host check, so an anonymous 401 establishes nothing about the allowlist.

An earlier version of this file kept the recorded request on a class attribute.
It leaked between tests: deleting bare-hostname support outright still left
``test_bare_hostname_is_read_as_https`` green, reading a value the previous test
had written. The recorder is per-instance now and every assertion goes through
the instance the check actually used.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "unitares_doctor.py"

_GATE_ENV = (
    "UNITARES_DOCTOR_PUBLIC_URL",
    "UNITARES_OAUTH_ISSUER_URL",
    "UNITARES_MCP_BEARER_TOKENS",
)


@pytest.fixture(scope="module")
def doctor():
    spec = importlib.util.spec_from_file_location("unitares_doctor", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unitares_doctor"] = mod  # Python 3.14 dataclass needs this
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The host machine may carry any of these; start every test from unset."""
    for var in _GATE_ENV:
        monkeypatch.delenv(var, raising=False)


class _FakeConn:
    """Stands in for http.client.HTTPConnection, recording what was sent."""

    def __init__(self, status: int | None = None, exc: Exception | None = None):
        self._status = status
        self._exc = exc
        self.sent: dict | None = None

    def request(self, method, path, headers=None):
        if self._exc is not None:
            raise self._exc
        self.sent = {"method": method, "path": path, "headers": headers or {}}

    def getresponse(self):
        return SimpleNamespace(status=self._status)

    def close(self):
        pass


def _run(doctor, *, status=None, exc=None):
    """Run the check against a fake connection. Returns (result, conn, ctor)."""
    conn = _FakeConn(status=status, exc=exc)
    with patch("http.client.HTTPConnection") as ctor:
        ctor.return_value = conn
        result = doctor.check_mcp_route_gate()
    return result, conn, ctor


def _headers(conn: _FakeConn) -> dict:
    assert conn.sent is not None, "the check never issued a request"
    return conn.sent["headers"]


# --- configuration ----------------------------------------------------------

def test_skips_when_no_public_host_is_configured(doctor):
    result = doctor.check_mcp_route_gate()
    assert result.status is doctor.Status.SKIP
    assert "UNITARES_DOCTOR_PUBLIC_URL" in result.detail


def test_doctor_public_url_wins_over_issuer(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_OAUTH_ISSUER_URL", "https://issuer.example.org")
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "https://tunnel.example.org")
    _, conn, _ = _run(doctor, status=400)
    assert _headers(conn)["Host"] == "tunnel.example.org"


def test_bare_hostname_is_read_as_https(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    _, conn, _ = _run(doctor, status=400)
    assert _headers(conn)["Host"] == "gov.example.org"


def test_port_is_preserved_in_the_host_header(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "https://gov.example.org:8443")
    _, conn, _ = _run(doctor, status=400)
    assert _headers(conn)["Host"] == "gov.example.org:8443"


def test_userinfo_is_stripped_from_the_host_and_the_output(doctor, monkeypatch):
    """A tunnel URL carrying basic-auth must not reach the header or the report."""
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "https://svc:s3cr3t@gov.example.org")
    result, conn, _ = _run(doctor, status=421)
    assert _headers(conn)["Host"] == "gov.example.org"
    assert "s3cr3t" not in result.message
    assert "s3cr3t" not in result.detail


def test_host_is_lowercased_to_match_an_exact_allowlist(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "https://GOV.Example.ORG")
    _, conn, _ = _run(doctor, status=400)
    assert _headers(conn)["Host"] == "gov.example.org"


# --- the probe's shape ------------------------------------------------------

def test_probe_carries_the_external_host_to_the_local_listener(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_OAUTH_ISSUER_URL", "https://gov.example.org")
    _, conn, ctor = _run(doctor, status=400)
    assert _headers(conn)["Host"] == "gov.example.org"
    assert conn.sent["path"] == doctor.MCP_ROUTE_PATH
    # Connecting to the external host would make this a tunnel test.
    assert ctor.call_args.args[:2] == ("127.0.0.1", 8767)


def test_probe_sends_a_visible_bearer_token(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", " tok-one , tok-two ")
    _, conn, _ = _run(doctor, status=400)
    assert _headers(conn)["Authorization"] == "Bearer tok-one"


def test_probe_omits_authorization_when_no_token_is_visible(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    _, conn, _ = _run(doctor, status=400)
    assert "Authorization" not in _headers(conn)


# --- classification ---------------------------------------------------------

def test_421_fails_and_names_the_allowlist(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_OAUTH_ISSUER_URL", "https://gov.example.org")
    result, _, _ = _run(doctor, status=421)
    assert result.status is doctor.Status.FAIL
    assert "421" in result.message
    assert "UNITARES_MCP_ALLOWED_HOSTS" in result.detail


def test_anonymous_401_is_inconclusive_never_a_pass(doctor, monkeypatch):
    """The auth gate answers before the Host is validated, so 401 proves nothing."""
    monkeypatch.setenv("UNITARES_OAUTH_ISSUER_URL", "https://gov.example.org")
    result, _, _ = _run(doctor, status=401)
    assert result.status is doctor.Status.WARN
    assert result.status is not doctor.Status.PASS
    assert "inconclusive" in result.message


def test_401_with_a_token_is_a_rejected_credential(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "wrong-token")
    result, _, _ = _run(doctor, status=401)
    assert result.status is doctor.Status.FAIL
    assert "token" in result.message


@pytest.mark.parametrize("status", [200, 400])
def test_authenticated_probe_past_the_host_gate_passes(doctor, monkeypatch, status):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "tok")
    result, _, _ = _run(doctor, status=status)
    assert result.status is doctor.Status.PASS


@pytest.mark.parametrize("status", [200, 400])
def test_unchallenged_anonymous_probe_warns_that_the_route_is_ungated(
    doctor, monkeypatch, status
):
    """Observed behaviour, not config intent: no challenge means no gate.

    This is the case a config read got wrong. With the issuer variable set but
    provider construction failed at startup, the gate is off while the variable
    still says it should be on.
    """
    monkeypatch.setenv("UNITARES_OAUTH_ISSUER_URL", "https://gov.example.org")
    result, _, _ = _run(doctor, status=status)
    assert result.status is doctor.Status.WARN
    assert "without challenging an anonymous caller" in result.message


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_error_statuses_fail_rather_than_reading_as_reachable(doctor, monkeypatch, status):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "tok")
    result, _, _ = _run(doctor, status=status)
    assert result.status is doctor.Status.FAIL
    assert str(status) in result.message


def test_403_does_not_claim_the_host_allowlist_is_the_remedy(doctor, monkeypatch):
    """The SDK answers 421 for a rejected Host and 403 only for a rejected Origin."""
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    result, _, _ = _run(doctor, status=403)
    assert "UNITARES_MCP_ALLOWED_HOSTS" not in result.detail


def test_unclassified_status_warns(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    result, _, _ = _run(doctor, status=302)
    assert result.status is doctor.Status.WARN


# --- transport failures -----------------------------------------------------

def test_timeout_is_a_latency_finding_not_a_down_finding(doctor, monkeypatch):
    """Mirrors check_http_health; 'unreachable' would misdirect the operator."""
    import socket
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    result, _, _ = _run(doctor, exc=socket.timeout("timed out"))
    assert result.status is doctor.Status.WARN
    assert "did not respond" in result.message


def test_refused_connection_fails(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    result, _, _ = _run(doctor, exc=ConnectionRefusedError("nothing on 8767"))
    assert result.status is doctor.Status.FAIL
    assert "unreachable" in result.message


def test_malformed_response_is_caught_not_raised(doctor, monkeypatch):
    """http.client.HTTPException is not an OSError and would escape a naive tuple."""
    import http.client
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    result, _, _ = _run(doctor, exc=http.client.BadStatusLine("garbage"))
    assert result.status is doctor.Status.FAIL


# --- registration -----------------------------------------------------------

def test_registered_as_an_operator_check(doctor):
    checks = doctor.build_checks(
        repo_root=REPO_ROOT,
        db_url="postgresql://localhost/governance",
    )
    gate = [c for c in checks if c.name == "mcp_route_gate"]
    assert len(gate) == 1
    assert gate[0].mode == "operator"
