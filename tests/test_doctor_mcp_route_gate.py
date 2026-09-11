"""Tests for the doctor's MCP-route gate check.

The check exists because /health and /mcp/ disagree: DNS-rebinding protection
validates the Host on the MCP route only, so a deployment whose allowlist is
missing the public hostname serves 421 on every MCP call while every health
surface reads 200. These tests pin the classification of each gate and, most
importantly, that the probe actually carries the external Host — a probe that
sent the loopback Host would pass on exactly the broken deployment the check
is for.
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

    sent: dict = {}

    def __init__(self, status: int | None = None, exc: Exception | None = None):
        self._status = status
        self._exc = exc

    def request(self, method, path, headers=None):
        if self._exc is not None:
            raise self._exc
        type(self).sent = {"method": method, "path": path, "headers": headers or {}}

    def getresponse(self):
        return SimpleNamespace(status=self._status)

    def close(self):
        pass


def _run(doctor, *, status=None, exc=None):
    conn = _FakeConn(status=status, exc=exc)
    with patch("http.client.HTTPConnection", return_value=conn):
        return doctor.check_mcp_route_gate()


def test_skips_when_no_public_host_is_configured(doctor):
    result = doctor.check_mcp_route_gate()
    assert result.status is doctor.Status.SKIP
    assert "UNITARES_DOCTOR_PUBLIC_URL" in result.detail


def test_probe_carries_the_external_host_not_loopback(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_OAUTH_ISSUER_URL", "https://gov.example.org")
    _run(doctor, status=401)
    headers = _FakeConn.sent["headers"]
    assert headers["Host"] == "gov.example.org"
    assert _FakeConn.sent["path"] == doctor.MCP_ROUTE_PATH


def test_421_fails_and_names_the_allowlist(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_OAUTH_ISSUER_URL", "https://gov.example.org")
    result = _run(doctor, status=421)
    assert result.status is doctor.Status.FAIL
    assert "421" in result.message
    assert "UNITARES_MCP_ALLOWED_HOSTS" in result.detail


def test_403_fails_as_the_host_gate(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "https://gov.example.org")
    result = _run(doctor, status=403)
    assert result.status is doctor.Status.FAIL
    assert "403" in result.message


def test_401_passes_because_an_answering_auth_gate_is_healthy(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_OAUTH_ISSUER_URL", "https://gov.example.org")
    result = _run(doctor, status=401)
    assert result.status is doctor.Status.PASS


def test_reachable_but_ungated_warns(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    result = _run(doctor, status=200)
    assert result.status is doctor.Status.WARN
    assert "no auth gate configured" in result.message


def test_reachable_with_bearer_configured_passes(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "deadbeef")
    result = _run(doctor, status=200)
    assert result.status is doctor.Status.PASS


def test_bare_hostname_is_read_as_https(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    _run(doctor, status=401)
    assert _FakeConn.sent["headers"]["Host"] == "gov.example.org"


def test_doctor_public_url_wins_over_issuer(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_OAUTH_ISSUER_URL", "https://issuer.example.org")
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "https://tunnel.example.org")
    _run(doctor, status=401)
    assert _FakeConn.sent["headers"]["Host"] == "tunnel.example.org"


def test_unreachable_listener_fails(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_DOCTOR_PUBLIC_URL", "gov.example.org")
    result = _run(doctor, exc=ConnectionRefusedError("nothing on 8767"))
    assert result.status is doctor.Status.FAIL
    assert "unreachable" in result.message


def test_registered_as_an_operator_check(doctor):
    checks = doctor.build_checks(
        repo_root=REPO_ROOT,
        db_url="postgresql://localhost/governance",
    )
    gate = [c for c in checks if c.name == "mcp_route_gate"]
    assert len(gate) == 1
    assert gate[0].mode == "operator"
