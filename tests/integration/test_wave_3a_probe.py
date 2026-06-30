"""
Wave 3a probe-endpoint integration tests (PR #1 of Wave 3a sequencing).

Specification:
    ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §2.2 (envelope),
    §2.3 (endpoint list), §2.5 (bearer auth), §2.6 (timestamp masking).

Test surface:
    A minimal Starlette ASGI app with the Wave 3a probe routes mounted via
    ``register_wave3a_probe_routes``. The MCP server is NOT started — these
    tests exercise the route handlers directly through Starlette's
    ``TestClient``, the same pattern used by
    ``tests/test_http_endpoints.py``.

Coverage (8 cases):
    1. Token unset -> 503 on every /v1/probe/* endpoint (fail-closed §2.5).
    2. Wrong token -> 401 on every endpoint.
    3. Missing Authorization header -> 401 on every endpoint.
    4. Correct token -> 200 with top-level-keys envelope (§2.2) on every
       endpoint.
    5. ``protocol_version`` is exactly ``wave3a.v1`` on every response.
    6. Timestamp masking: ``tool_registry`` byte-equality across two calls
       (FIND-R2 fold — guards against future ``list_all_aliases``-style
       non-determinism).
    7. ``/v1/probe/health`` has no ``data`` key (bare liveness).
    8. ``/v1/probe/server_info`` has ``meta.probe_process: true``
       (FIND-R3 fold — BEAM caller knows the PID/transport identifies the
       Python probe).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.mcp_handlers import wave3a_probe
from src.mcp_handlers.wave3a_probe import (
    PROBE_PREFIX,
    PROBE_TOKEN_ENV,
    PROTOCOL_VERSION,
    register_wave3a_probe_routes,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


VALID_TOKEN = "wave3a-test-token-correct"
WRONG_TOKEN = "wave3a-test-token-wrong"


@pytest.fixture
def app() -> Starlette:
    """Bare Starlette app with only the probe routes mounted."""
    app = Starlette(routes=[])
    register_wave3a_probe_routes(app)
    return app


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


@pytest.fixture
def token_set(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(PROBE_TOKEN_ENV, VALID_TOKEN)
    return VALID_TOKEN


@pytest.fixture
def token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROBE_TOKEN_ENV, raising=False)


# Endpoints subject to bearer-auth (all of §2.3 except /health).
AUTHED_ENDPOINTS = [
    f"{PROBE_PREFIX}/health_snapshot",
    f"{PROBE_PREFIX}/server_info",
    f"{PROBE_PREFIX}/tool_registry",
    f"{PROBE_PREFIX}/list_tools",
    f"{PROBE_PREFIX}/describe_tool",
]

LIVENESS_ENDPOINT = f"{PROBE_PREFIX}/health"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Case 1 — fail-closed when token unset
# ---------------------------------------------------------------------------


class TestTokenUnset:
    """§2.5: missing WAVE_3A_PROBE_TOKEN -> 503 on every authed endpoint."""

    def test_authed_endpoints_return_503_when_token_unset(
        self, client: TestClient, token_unset: None
    ) -> None:
        for path in AUTHED_ENDPOINTS:
            response = client.get(path, headers=_bearer(VALID_TOKEN))
            assert response.status_code == 503, f"{path} did not 503"
            body = response.json()
            assert body["ok"] is False
            assert body["protocol_version"] == PROTOCOL_VERSION
            assert body["error"] == "service_unavailable"
            assert "WAVE_3A_PROBE_TOKEN" in body["reason"]


# ---------------------------------------------------------------------------
# Case 2 — wrong bearer token
# ---------------------------------------------------------------------------


class TestWrongToken:
    """§2.5: bearer token does not match -> 401."""

    def test_authed_endpoints_return_401_when_token_wrong(
        self, client: TestClient, token_set: str
    ) -> None:
        for path in AUTHED_ENDPOINTS:
            response = client.get(path, headers=_bearer(WRONG_TOKEN))
            assert response.status_code == 401, f"{path} did not 401"
            body = response.json()
            assert body["ok"] is False
            assert body["protocol_version"] == PROTOCOL_VERSION
            assert body["error"] == "permission_denied"
            assert body["reason"] == "bearer token missing or invalid"


# ---------------------------------------------------------------------------
# Case 3 — missing Authorization header
# ---------------------------------------------------------------------------


class TestMissingAuthHeader:
    """§2.5: Authorization header absent -> 401 (when token is set)."""

    def test_authed_endpoints_return_401_when_header_missing(
        self, client: TestClient, token_set: str
    ) -> None:
        for path in AUTHED_ENDPOINTS:
            response = client.get(path)  # no headers
            assert response.status_code == 401, f"{path} did not 401"
            body = response.json()
            assert body["ok"] is False
            assert body["protocol_version"] == PROTOCOL_VERSION
            assert body["error"] == "permission_denied"


# ---------------------------------------------------------------------------
# Case 4 — correct token returns 200 + top-level envelope
# ---------------------------------------------------------------------------


class TestAuthedEnvelope:
    """§2.2: top-level keys ok / protocol_version / data; no nested wrapper."""

    def test_health_snapshot_envelope(
        self, client: TestClient, token_set: str, snapshot_stub: None
    ) -> None:
        response = client.get(
            f"{PROBE_PREFIX}/health_snapshot", headers=_bearer(VALID_TOKEN)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["protocol_version"] == PROTOCOL_VERSION
        assert "data" in body
        assert isinstance(body["data"], dict)
        # Forbid nested envelope: data["ok"] would mean a double-wrap.
        assert "ok" not in body["data"]
        assert "protocol_version" not in body["data"]

    def test_server_info_envelope(
        self, client: TestClient, token_set: str
    ) -> None:
        response = client.get(
            f"{PROBE_PREFIX}/server_info", headers=_bearer(VALID_TOKEN)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["protocol_version"] == PROTOCOL_VERSION
        assert "data" in body
        assert isinstance(body["data"], dict)
        assert "current_pid" in body["data"]

    def test_tool_registry_envelope(
        self, client: TestClient, token_set: str
    ) -> None:
        response = client.get(
            f"{PROBE_PREFIX}/tool_registry", headers=_bearer(VALID_TOKEN)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["protocol_version"] == PROTOCOL_VERSION
        assert "data" in body
        data = body["data"]
        for key in ("tools", "aliases", "tiers", "deprecated_tools"):
            assert key in data, f"missing top-level key {key}"


# ---------------------------------------------------------------------------
# Case 5 — protocol_version is exactly wave3a.v1
# ---------------------------------------------------------------------------


class TestProtocolVersion:

    def test_protocol_version_pinned_on_success(
        self, client: TestClient, token_set: str, snapshot_stub: None
    ) -> None:
        for path in [LIVENESS_ENDPOINT, *AUTHED_ENDPOINTS]:
            response = client.get(path, headers=_bearer(VALID_TOKEN))
            assert response.status_code == 200
            assert response.json()["protocol_version"] == "wave3a.v1"

    def test_protocol_version_pinned_on_auth_failure(
        self, client: TestClient, token_set: str
    ) -> None:
        for path in AUTHED_ENDPOINTS:
            response = client.get(path, headers=_bearer(WRONG_TOKEN))
            assert response.status_code == 401
            assert response.json()["protocol_version"] == "wave3a.v1"

    def test_protocol_version_pinned_on_fail_closed(
        self, client: TestClient, token_unset: None
    ) -> None:
        for path in AUTHED_ENDPOINTS:
            response = client.get(path, headers=_bearer(VALID_TOKEN))
            assert response.status_code == 503
            assert response.json()["protocol_version"] == "wave3a.v1"


# ---------------------------------------------------------------------------
# Case 6 — timestamp masking yields byte-deterministic tool_registry
# ---------------------------------------------------------------------------


class TestToolRegistryMaskingDeterminism:
    """§2.6 + FIND-R2 fold: tool_registry must be byte-equal across calls.

    If a future change introduces a non-deterministic field (timestamp,
    UUID, monotonic counter, PID) into the tool_registry payload that the
    ``mask_timestamps`` regex set misses, this test fails. That is the
    intended signal: extend the regex set in ``wave3a_probe.py`` to cover
    the new field.
    """

    def test_two_calls_produce_byte_equal_body(
        self, client: TestClient, token_set: str
    ) -> None:
        first = client.get(
            f"{PROBE_PREFIX}/tool_registry", headers=_bearer(VALID_TOKEN)
        )
        second = client.get(
            f"{PROBE_PREFIX}/tool_registry", headers=_bearer(VALID_TOKEN)
        )
        assert first.status_code == 200
        assert second.status_code == 200
        # Compare canonicalized JSON to ignore key ordering differences
        # produced by Starlette's JSONResponse.
        import json

        first_norm = json.dumps(first.json(), sort_keys=True)
        second_norm = json.dumps(second.json(), sort_keys=True)
        assert first_norm == second_norm, (
            "tool_registry response differed across calls — masking regex "
            "missed a non-deterministic field; extend _VOLATILE_FIELD_NAMES "
            "or _VOLATILE_SUFFIXES in src/mcp_handlers/wave3a_probe.py"
        )


# ---------------------------------------------------------------------------
# Case 7 — /v1/probe/health is bare liveness
# ---------------------------------------------------------------------------


class TestLivenessShape:

    def test_health_has_no_data_key(self, client: TestClient) -> None:
        response = client.get(LIVENESS_ENDPOINT)
        assert response.status_code == 200
        body = response.json()
        assert body == {"ok": True, "protocol_version": "wave3a.v1"}
        assert "data" not in body

    def test_health_does_not_require_auth(
        self, client: TestClient, token_unset: None
    ) -> None:
        """Liveness probes must work even before the operator configures the
        probe token — that's the point of having a bare liveness endpoint."""
        response = client.get(LIVENESS_ENDPOINT)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Case 8 — server_info carries meta.probe_process: true
# ---------------------------------------------------------------------------


class TestServerInfoProbeProcessFlag:
    """FIND-R3 fold: meta.probe_process: true tells the BEAM caller that the
    PID/transport fields describe the Python probe, not the BEAM listener."""

    def test_meta_probe_process_true(
        self, client: TestClient, token_set: str
    ) -> None:
        response = client.get(
            f"{PROBE_PREFIX}/server_info", headers=_bearer(VALID_TOKEN)
        )
        assert response.status_code == 200
        body = response.json()
        assert "meta" in body
        assert body["meta"].get("probe_process") is True


# ---------------------------------------------------------------------------
# Case 9 — list_tools / describe_tool envelope + parity-source (PR #7/#8)
# ---------------------------------------------------------------------------


class TestListToolsEnvelope:
    """list_tools probe single-sources its payload by CALLING the in-process
    ``handle_list_tools`` and surfacing its output verbatim (§2.6 parity)."""

    def test_list_tools_envelope(self, client: TestClient, token_set: str) -> None:
        response = client.get(
            f"{PROBE_PREFIX}/list_tools", headers=_bearer(VALID_TOKEN)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["protocol_version"] == PROTOCOL_VERSION
        assert "data" in body
        data = body["data"]
        assert isinstance(data, dict)
        # Forbid a nested envelope.
        assert "ok" not in data
        assert "protocol_version" not in data
        # The handler's own payload rode through: lite list_tools carries
        # `tools` and the `success_response` envelope `success` flag.
        assert "tools" in data
        assert data.get("success") is True
        # `server_time` is masked for byte-determinism (§2.6).
        assert data.get("server_time") == "<MASKED_TIMESTAMP>"


class TestDescribeToolEnvelope:
    """describe_tool probe forwards ``tool_name`` to the in-process
    ``handle_describe_tool`` and surfaces both success and semantic-error
    shapes verbatim."""

    def test_describe_tool_known_tool(
        self, client: TestClient, token_set: str
    ) -> None:
        response = client.get(
            f"{PROBE_PREFIX}/describe_tool",
            params={"tool_name": "list_tools"},
            headers=_bearer(VALID_TOKEN),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["protocol_version"] == PROTOCOL_VERSION
        data = body["data"]
        assert data.get("success") is True
        assert data.get("tool") == "list_tools"
        assert "parameters" in data

    def test_describe_tool_missing_name_returns_canonical_error(
        self, client: TestClient, token_set: str
    ) -> None:
        # No tool_name query param → the handler's own `error_response`
        # ("tool_name is required") rides through `data`. The probe envelope
        # is still ok=True (the probe call itself succeeded); the semantic
        # failure is inside `data.success`.
        response = client.get(
            f"{PROBE_PREFIX}/describe_tool", headers=_bearer(VALID_TOKEN)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        data = body["data"]
        assert data.get("success") is False
        assert "tool_name is required" in data.get("error", "")

    def test_describe_tool_unknown_tool_returns_canonical_error(
        self, client: TestClient, token_set: str
    ) -> None:
        response = client.get(
            f"{PROBE_PREFIX}/describe_tool",
            params={"tool_name": "no_such_tool_xyz"},
            headers=_bearer(VALID_TOKEN),
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data.get("success") is False
        assert "Unknown tool" in data.get("error", "")


class TestListToolsDescribeToolDeterminism:
    """§2.6 + determinism: list_tools / describe_tool must be byte-equal
    across two calls (the masked `server_time` is the only volatile field;
    `agent_signature` is the deterministic unbound `{"uuid": null}`). Same
    contract as the tool_registry determinism guard."""

    def test_list_tools_two_calls_byte_equal(
        self, client: TestClient, token_set: str
    ) -> None:
        import json

        first = client.get(
            f"{PROBE_PREFIX}/list_tools", headers=_bearer(VALID_TOKEN)
        )
        second = client.get(
            f"{PROBE_PREFIX}/list_tools", headers=_bearer(VALID_TOKEN)
        )
        assert first.status_code == 200 and second.status_code == 200
        assert json.dumps(first.json(), sort_keys=True) == json.dumps(
            second.json(), sort_keys=True
        ), (
            "list_tools response differed across calls — a non-deterministic "
            "field escaped masking; extend _VOLATILE_FIELD_NAMES or "
            "_VOLATILE_SUFFIXES in src/mcp_handlers/wave3a_probe.py"
        )

    def test_describe_tool_two_calls_byte_equal(
        self, client: TestClient, token_set: str
    ) -> None:
        import json

        first = client.get(
            f"{PROBE_PREFIX}/describe_tool",
            params={"tool_name": "list_tools"},
            headers=_bearer(VALID_TOKEN),
        )
        second = client.get(
            f"{PROBE_PREFIX}/describe_tool",
            params={"tool_name": "list_tools"},
            headers=_bearer(VALID_TOKEN),
        )
        assert first.status_code == 200 and second.status_code == 200
        assert json.dumps(first.json(), sort_keys=True) == json.dumps(
            second.json(), sort_keys=True
        )


# ---------------------------------------------------------------------------
# Supporting helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def snapshot_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a deterministic health snapshot so /health_snapshot returns 200.

    The real snapshot is populated by ``deep_health_probe_task`` running in
    the background; tests don't have that task wired, so we stub the
    accessor used by the probe handler.
    """
    from src.services import health_snapshot as hs

    fake_snapshot = {
        "status": "ok",
        "version": "test",
        "checks": {"db": {"status": "ok"}},
    }

    def fake_get_snapshot():
        return fake_snapshot, 1.0, 1234567890.0

    monkeypatch.setattr(hs, "get_snapshot", fake_get_snapshot)
    # is_stale stays as-is (cheap pure function); PROBE_INTERVAL_SECONDS and
    # STALENESS_THRESHOLD_SECONDS are module constants the handler reads.
