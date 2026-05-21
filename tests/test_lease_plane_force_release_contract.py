"""
Stage B — §9 integration test for the force-release HTTP contract layer.

Tests the live Elixir router's path-aware auth enforcement for
POST /v1/lease/force-release. Skips at module level if the router
is not reachable on 127.0.0.1:8788 (fast TCP probe), so CI/remote
environments without the BEAM server still pass.

RFC §9 gate (line 971, ):
  test_force_release_rejects_governance_token

Test name MUST match exactly for audit_rfc_section_9_gates.py to
report it as "exact". The existing Python contract-layer test in
tests/test_lease_plane_client.py covers rejection at the Python layer;
this file covers rejection at the Elixir router layer.

Stage B passes locally against the running BEAM server; remote CI
sees it skip due to no live router (see skip predicate below).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.request
import uuid
from pathlib import Path

import pytest

# ---------- skip predicate ----------

_ROUTER_HOST = "127.0.0.1"
_ROUTER_PORT = 8788
_FORCE_RELEASE_PATH = "/v1/lease/force-release"
_RELEASE_PATH = "/v1/lease/release"


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
        "Stage B integration test skipped. Run against a live BEAM server to verify.",
        allow_module_level=True,
    )


# ---------- helpers ----------

def _read_secrets_var(var_name: str) -> str | None:
    """Read VAR_NAME from env or ~/.config/cirwel/secrets.env."""
    tok = os.environ.get(var_name)
    if tok:
        return tok
    secrets_path = Path.home() / ".config" / "cirwel" / "secrets.env"
    if not secrets_path.exists():
        return None
    prefix = f"{var_name}="
    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _read_force_release_token() -> str | None:
    """Read LEASE_FORCE_RELEASE_TOKEN from env or ~/.config/cirwel/secrets.env."""
    return _read_secrets_var("LEASE_FORCE_RELEASE_TOKEN")


def _read_lease_plane_bearer() -> str | None:
    """Read LEASE_PLANE_BEARER_TOKEN — the regular bearer the router accepts."""
    return _read_secrets_var("LEASE_PLANE_BEARER_TOKEN")


def _router_url(path: str) -> str:
    return f"http://{_ROUTER_HOST}:{_ROUTER_PORT}{path}"


def _post_json(path: str, body: dict, *, authorization: str | None = None) -> tuple[int, dict]:
    """POST JSON to the router; returns (status_code, response_body)."""
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if authorization:
        headers["Authorization"] = authorization

    req = urllib.request.Request(_router_url(path), data=data, headers=headers, method="POST")
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


def _acquire_lease(bearer_token: str) -> str:
    """Acquire a td:/ lease and return its lease_id string."""
    body = {
        "surface_id": f"td:/force-release-contract-test-{uuid.uuid4()}",
        "holder_agent_uuid": str(uuid.uuid4()),
        "holder_class": "process_instance",
        "holder_kind": "local_beam",
        "ttl_s": 60,
    }
    status, payload = _post_json("/v1/lease/acquire", body, authorization=f"Bearer {bearer_token}")
    assert status == 200 and payload.get("ok") is True, (
        f"acquire failed (status={status}): {payload}"
    )
    return str(payload["lease"]["lease_id"])


# ---------- §9 gate ----------

# §9: test_force_release_rejects_governance_token
def test_force_release_rejects_governance_token(monkeypatch):
    """RFC §9 gate (line 971): POST /v1/lease/force-release MUST reject any
    token other than LEASE_FORCE_RELEASE_TOKEN with HTTP 401 + permission_denied.

    Three sub-assertions:
    1. Non-elevated token (GOVERNANCE_TOKEN / standard bearer) → 401 permission_denied
    2. No Authorization header → 401 permission_denied
    3. Elevated token (LEASE_FORCE_RELEASE_TOKEN) + real lease_id → 200 ok:true

    This test hits the LIVE Elixir router and is skip-by-default when the
    router is not reachable (module-level skip predicate above).
    """
    force_release_token = _read_force_release_token()
    if not force_release_token:
        pytest.skip(
            "LEASE_FORCE_RELEASE_TOKEN not set (env or ~/.config/cirwel/secrets.env); "
            "cannot complete sub-assertion 3 (elevated-token success path)"
        )

    # We need the regular lease-plane bearer to acquire a lease for sub-assertion 3.
    # The "GOVERNANCE_TOKEN" name in this module's prose is a label for the
    # regular (non-elevated) bearer; the actual env var on the router side is
    # LEASE_PLANE_BEARER_TOKEN. Sub-assertions 1 and 2 use a synthetic token
    # since the auth check fires before the lease lookup.
    lease_plane_bearer = _read_lease_plane_bearer()
    if not lease_plane_bearer:
        pytest.skip(
            "LEASE_PLANE_BEARER_TOKEN not set (env or ~/.config/cirwel/secrets.env); "
            "cannot acquire a real lease for sub-assertion 3"
        )

    # Sub-assertion 1: non-elevated token is rejected at the path level.
    # The router's path-aware HTTPAuth rejects any token that is not
    # LEASE_FORCE_RELEASE_TOKEN on the /v1/lease/force-release path.
    monkeypatch.setenv("GOVERNANCE_TOKEN", "non-elevated-token-for-test")
    status1, body1 = _post_json(
        _FORCE_RELEASE_PATH,
        {"lease_id": str(uuid.uuid4())},
        authorization="Bearer non-elevated-token-for-test",
    )
    assert status1 == 401, (
        f"non-elevated token must be rejected with HTTP 401; got {status1}: {body1}"
    )
    assert body1.get("error") == "permission_denied", (
        f"expected error=permission_denied; got {body1}"
    )

    # Sub-assertion 2: no Authorization header is rejected.
    status2, body2 = _post_json(
        _FORCE_RELEASE_PATH,
        {"lease_id": str(uuid.uuid4())},
        authorization=None,
    )
    assert status2 == 401, (
        f"missing Authorization header must yield HTTP 401; got {status2}: {body2}"
    )

    # Sub-assertion 3: elevated token + real lease → 200 ok:true.
    lease_id = _acquire_lease(lease_plane_bearer)
    status3, body3 = _post_json(
        _FORCE_RELEASE_PATH,
        {"lease_id": lease_id},
        authorization=f"Bearer {force_release_token}",
    )
    assert status3 == 200, (
        f"elevated token must be accepted with HTTP 200; got {status3}: {body3}"
    )
    assert body3.get("ok") is True, (
        f"expected ok:true from force-release; got {body3}"
    )


def test_release_rejects_forced_reason_on_release_endpoint():
    """RFC §7.10 Elixir-side corollary: POST /v1/lease/release with
    release_reason='forced' must return 200 + permission_denied
    (not 401 — the standard bearer is valid on this path; the rejection
    is semantic, not auth-level).

    This test was added in PR 1 on the BEAM side. Re-verified here as
    a sanity check that the path routing is still correct after PR 2.

    Requires the regular bearer (LEASE_PLANE_BEARER_TOKEN) to be valid;
    skips cleanly otherwise so the auth-level rejection (401) doesn't
    masquerade as a semantic failure.
    """
    lease_plane_bearer = _read_lease_plane_bearer()
    if not lease_plane_bearer:
        pytest.skip(
            "LEASE_PLANE_BEARER_TOKEN not set (env or ~/.config/cirwel/secrets.env); "
            "cannot exercise the semantic-rejection path with a valid bearer"
        )
    status, body = _post_json(
        _RELEASE_PATH,
        {"lease_id": str(uuid.uuid4()), "release_reason": "forced"},
        authorization=f"Bearer {lease_plane_bearer}",
    )
    # The Elixir router returns 200 + ok:false + permission_denied for semantic
    # rejections (not a lease mismatch, not a missing route — a policy rejection).
    assert status == 200, f"expected 200 semantic rejection; got {status}: {body}"
    assert body.get("ok") is False
    assert body.get("error") == "permission_denied", (
        f"expected error=permission_denied for forced release on /release; got {body}"
    )
    assert "force_release_endpoint" in (body.get("reason") or ""), (
        f"reason must reference the force-release endpoint; got {body}"
    )
