"""Wave 2 §"Lease-integration boundary hardening" — auth-rejection envelope contract.

Pins a structural contract at the LIVE Elixir router boundary: every
auth-rejected response on every `/v1/lease/*` (and `/v1/health`) endpoint
must carry the `protocol_version` field in the JSON envelope, even though
the request never reaches a handler.

Why this matters
----------------
The `protocol_version` handshake (PROTOCOL_VERSION = "v1.0" — pinned in
`test_lease_plane_protocol_version.py`) is how the Python client detects
drift between deploys without forcing coordinated client/server rolls.
The Python side warns on mismatch and is silent on absence (older BEAM,
grace window). But that grace path means: if the BEAM router ever
*regresses* and stops emitting `protocol_version` on auth-rejection
paths, the Python client will silently treat those rejections as
"old server, no version" and the operator loses drift visibility on
exactly the responses where drift matters most (auth changes are the
highest-risk redeploys).

The existing test_lease_plane_protocol_version tests pin Python-side
client behavior with mocked responses. The Elixir test
(`elixir/lease_plane/test/lease_plane_protocol_version_test.exs`,
referenced in the protocol-version test docstring) pins emission on the
HAPPY paths from the server side. Neither pins the cross-boundary
contract that ERROR envelopes — specifically auth-rejection envelopes
which are produced by the HTTPAuth plug, BEFORE the handler that
normally injects protocol_version — also carry the version field.

This test fills that gap. It's a live-boundary contract test, mirroring
the conventions of `test_lease_plane_force_release_contract.py`:
module-level skip when 127.0.0.1:8788 isn't reachable, no test fixtures
required, no DB writes.

Scope
-----
Stays tight on the "every error response should carry protocol_version"
contract operator named explicitly. Does not cover:
- Happy-path envelopes (covered by health-check and force-release tests).
- 401-vs-503 fail-closed when LEASE_PLANE_BEARER_TOKEN is unset
  (separate concern; depends on server config and would require
  router restart to verify).
- Conflict envelopes (held_by_other) — those need a real prior holder
  to provoke; out of scope for an auth-envelope test.
- The `/v1/lease/force-release` endpoint — it has a dedicated test in
  `test_lease_plane_force_release_contract.py` that handles the
  elevated-token env load. Including it here would either skip when
  LEASE_FORCE_RELEASE_TOKEN is unconfigured (most CI) or fail with a
  503 "force-release token not configured" envelope rather than the
  401 envelope this test is pinning. The 503 envelope also carries
  `protocol_version`, but that's a different contract (fail-closed
  config envelope, not auth-plug envelope) and belongs in a
  separate fail-closed envelope test if it's worth pinning.

If this test fails, the fix is on the BEAM side: the HTTPAuth plug
must call the same `protocol_version/0` injection as the handlers.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from src.lease_plane import PROTOCOL_VERSION

# ---------- skip predicate ----------

_ROUTER_HOST = "127.0.0.1"
_ROUTER_PORT = 8788


def _router_reachable() -> bool:
    """Fast TCP probe — returns True if the Elixir router is listening."""
    try:
        with socket.create_connection((_ROUTER_HOST, _ROUTER_PORT), timeout=0.5):
            return True
    except OSError:
        return False


if not _router_reachable():
    pytest.skip(
        f"Elixir lease-plane router not reachable at {_ROUTER_HOST}:{_ROUTER_PORT}; "
        "auth-envelope contract test skipped. Run against a live BEAM server to verify.",
        allow_module_level=True,
    )


# ---------- helpers ----------


def _router_url(path: str) -> str:
    return f"http://{_ROUTER_HOST}:{_ROUTER_PORT}{path}"


def _request(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    authorization: str | None = None,
) -> tuple[int, dict]:
    """Issue an HTTP request to the router; returns (status_code, json_body).

    Returns an empty dict for non-JSON bodies — callers that need to
    assert on envelope shape should fail loudly when the body isn't a
    JSON object.
    """
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if authorization:
        headers["Authorization"] = authorization

    req = urllib.request.Request(_router_url(path), data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    return status, payload


# ---------- contract ----------


# Each tuple: (label, method, path, body) — covers every auth-protected
# `/v1/lease/*` endpoint plus the health probe. The body shape doesn't
# matter for the auth-rejection path because HTTPAuth runs before the
# handler validates the body; we still send a syntactically valid body
# so a future router change that validates body BEFORE auth would surface
# as a schema_invalid response (different error discriminant) rather than
# a misleading 400 from missing fields.
_AUTH_PROTECTED_ENDPOINTS: list[tuple[str, str, str, dict | None]] = [
    (
        "acquire",
        "POST",
        "/v1/lease/acquire",
        {
            "surface_id": "td:/auth-envelope-contract-test",
            "holder_agent_uuid": "00000000-0000-0000-0000-000000000001",
            "holder_class": "process_instance",
            "holder_kind": "local_beam",
            "ttl_s": 60,
        },
    ),
    (
        "release",
        "POST",
        "/v1/lease/release",
        {"lease_id": "00000000-0000-0000-0000-000000000001"},
    ),
    (
        "heartbeat",
        "POST",
        "/v1/lease/heartbeat",
        {"lease_id": "00000000-0000-0000-0000-000000000001"},
    ),
    (
        "status",
        "GET",
        "/v1/lease/status?lease_id=00000000-0000-0000-0000-000000000001",
        None,
    ),
    (
        "health",
        "GET",
        "/v1/health",
        None,
    ),
]


@pytest.mark.parametrize(
    ("label", "method", "path", "body"),
    _AUTH_PROTECTED_ENDPOINTS,
    ids=[t[0] for t in _AUTH_PROTECTED_ENDPOINTS],
)
def test_auth_rejection_envelope_carries_protocol_version_wrong_token(
    label: str, method: str, path: str, body: dict | None
) -> None:
    """Auth-rejection contract (wrong token): every `/v1/lease/*` and
    `/v1/health` endpoint must return a 401 envelope that carries the
    `protocol_version` field.

    The HTTPAuth plug runs before the handler. Handlers inject
    `protocol_version` via the router's `protocol_version/0` helper.
    If the plug does NOT also inject the field, every auth-rejection
    response loses the version envelope — and the Python client's
    `_check_protocol_version` treats absence as "older BEAM, grace
    window, stay silent." Auth rejections then become invisible to
    drift detection precisely when drift detection matters most
    (auth changes are the highest-risk redeploys).

    Tested per-endpoint so a single regressed route surfaces as a
    single parametrized failure, not a whole-file blowup.
    """
    status, payload = _request(
        method,
        path,
        body=body,
        authorization="Bearer wrong-token-for-envelope-contract-test",
    )

    # Contract: a syntactically-valid JSON object envelope, even on
    # auth rejection. A non-object body would mean the router fell
    # back to a generic error handler that doesn't speak this
    # protocol — also worth pinning.
    assert isinstance(payload, dict) and payload, (
        f"{label}: auth-rejection response must be a non-empty JSON object; "
        f"got status={status}, body={payload!r}"
    )

    # Contract: auth rejection is 401 with permission_denied. This is
    # already tested for /v1/lease/force-release in
    # test_lease_plane_force_release_contract.py; re-asserting here keeps
    # this test self-contained when one endpoint is touched without the
    # other test file being read.
    assert status == 401, (
        f"{label}: wrong bearer must be rejected with HTTP 401; got {status}: {payload}"
    )
    assert payload.get("ok") is False, (
        f"{label}: rejection envelope must carry ok:false; got {payload}"
    )
    assert payload.get("error") == "permission_denied", (
        f"{label}: rejection envelope must carry error=permission_denied; got {payload}"
    )

    # The actual contract being pinned by this file: protocol_version is
    # present on the auth-rejection envelope, equal to the version the
    # Python client constant pins.
    assert "protocol_version" in payload, (
        f"{label}: auth-rejection envelope is missing the protocol_version "
        f"field. The HTTPAuth plug must inject protocol_version on rejection "
        f"the same way handlers do, or the Python client cannot detect "
        f"version drift on auth failures (the responses where drift "
        f"detection matters most). Envelope: {payload}"
    )
    assert payload["protocol_version"] == PROTOCOL_VERSION, (
        f"{label}: auth-rejection envelope reports "
        f"protocol_version={payload['protocol_version']!r}, but the Python "
        f"client pins PROTOCOL_VERSION={PROTOCOL_VERSION!r}. If the BEAM "
        f"router intentionally bumped the version, the Python constant in "
        f"src/lease_plane/__init__.py must bump in the same PR (Stability "
        f"discipline)."
    )


@pytest.mark.parametrize(
    ("label", "method", "path", "body"),
    _AUTH_PROTECTED_ENDPOINTS,
    ids=[t[0] for t in _AUTH_PROTECTED_ENDPOINTS],
)
def test_auth_rejection_envelope_carries_protocol_version_no_header(
    label: str, method: str, path: str, body: dict | None
) -> None:
    """Same contract as the wrong-token test, but with no Authorization
    header at all. The HTTPAuth plug takes a different code path for
    "no header" vs "wrong token" (in `http_auth.ex` the former is a
    `nil` match, the latter is a string comparison). Pinning both
    explicitly catches a regression that touches only one branch.

    Parametrized to mirror the wrong-token coverage above.
    """
    status, payload = _request(method, path, body=body, authorization=None)

    assert isinstance(payload, dict) and payload, (
        f"{label}: missing-header rejection must be a non-empty JSON object; "
        f"got status={status}, body={payload!r}"
    )
    assert status == 401, (
        f"{label}: missing Authorization header must yield HTTP 401; got {status}: {payload}"
    )
    assert payload.get("ok") is False
    assert payload.get("error") == "permission_denied"

    assert "protocol_version" in payload, (
        f"{label}: missing-header rejection envelope is missing the "
        f"protocol_version field. See the wrong-token sibling test for the "
        f"full rationale — auth-plug paths must inject protocol_version "
        f"the same way handlers do. Envelope: {payload}"
    )
    assert payload["protocol_version"] == PROTOCOL_VERSION, (
        f"{label}: missing-header rejection reports "
        f"protocol_version={payload['protocol_version']!r}, expected "
        f"{PROTOCOL_VERSION!r}."
    )
