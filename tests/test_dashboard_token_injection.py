"""The dashboard shell must not hand its bearer token to anonymous callers.

The shell routes are public by design — the sign-in page has to be reachable
before anyone has a session. But they also stamp UNITARES_HTTP_API_TOKEN into
the served HTML so data.js can authenticate, and that injection was
unconditional: any caller who could fetch `/` received the local bearer in
plaintext, and could then authenticate to every gated route. On a deployment
whose port is fronted by a tunnel, "any caller" is the internet.

The page stays public; the credential does not.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_routes.dashboard import http_dashboard_redesign, http_phase

TOKEN = "sekrit-local-token"  # noqa: S105 - test fixture, not a real credential


@pytest.fixture(autouse=True)
def _local_posture(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", TOKEN)


def _client(peer):
    app = Starlette(routes=[
        Route("/", http_dashboard_redesign, methods=["GET"]),
        Route("/phase", http_phase, methods=["GET"]),
    ])
    return TestClient(app, client=peer)


@pytest.mark.parametrize("path", ["/", "/phase"])
def test_anonymous_caller_never_receives_the_token(path):
    r = _client(("203.0.113.7", 44444)).get(path)
    # The shell itself still serves — only the credential is withheld.
    assert r.status_code in (200, 404), f"{path} -> {r.status_code}"
    assert TOKEN not in r.text, f"{path} leaked the API token to an untrusted peer"


@pytest.mark.parametrize("path", ["/", "/phase"])
def test_trusted_peer_still_gets_the_convenience_token(path):
    """Unconditional: a test that passes when nothing was injected proves
    nothing about the positive path."""
    r = _client(("127.0.0.1", 50000)).get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert TOKEN in r.text, f"{path} withheld the token from a loopback caller"


@pytest.mark.parametrize("path", ["/", "/phase"])
def test_hosted_posture_withholds_from_rfc1918(path, monkeypatch):
    """Hosted mode has no trusted-network bypass — a cloud proxy peer (10.x)
    is not authenticated and must not be handed the token."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-secret")
    r = _client(("10.1.2.3", 44444)).get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert TOKEN not in r.text, f"{path} leaked the token to a 10.x peer in hosted mode"


@pytest.mark.parametrize("path", ["/", "/phase"])
def test_hosted_posture_withholds_even_from_loopback(path, monkeypatch):
    """Hosted mode accepts only the MCP bearer, so UNITARES_HTTP_API_TOKEN is a
    credential every gated route would reject. Injecting it would also make
    data.js believe it holds a token and skip the 401 -> sign-in redirect,
    stranding the operator on a silently stale page."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-secret")
    r = _client(("127.0.0.1", 50000)).get(path)
    assert r.status_code == 200
    assert TOKEN not in r.text, f"{path} injected the wrong credential in hosted mode"


@pytest.mark.parametrize("path", ["/", "/phase"])
def test_credential_bearing_shell_is_not_shared_cacheable(path):
    """The response varies by caller and can carry a credential, and this
    deployment sits behind a tunnel — a shared cache must not store it or
    serve one caller's copy to another."""
    r = _client(("127.0.0.1", 50000)).get(path)
    assert r.headers.get("Cache-Control") == "no-store", f"{path} is cacheable"
    vary = r.headers.get("Vary", "")
    assert "Cookie" in vary and "Authorization" in vary, f"{path} Vary={vary!r}"


# --- the bundled snapshot is data, not shell ---------------------------------

def _redesign_client(peer):
    app = Starlette(routes=[
        Route("/dashboard/redesign/{file:path}", http_dashboard_redesign, methods=["GET"]),
    ])
    return TestClient(app, client=peer)


def test_snapshot_bundle_is_not_public():
    """snapshot.js is a real capture — resident ids, EISV, verdicts — so it is
    gated like the endpoints it mirrors, even though it is a .js file under the
    otherwise-public shell."""
    r = _redesign_client(("203.0.113.7", 44444)).get("/dashboard/redesign/snapshot.js")
    assert r.status_code == 401
    assert "SNAPSHOT" not in r.text


def test_presentation_assets_stay_public():
    """The gate must not swallow the shell: styling still serves anonymously,
    or the sign-in page renders unstyled for someone who cannot yet sign in."""
    r = _redesign_client(("203.0.113.7", 44444)).get("/dashboard/redesign/tokens.css")
    assert r.status_code == 200


def test_snapshot_bundle_served_to_trusted_caller():
    r = _redesign_client(("127.0.0.1", 50000)).get("/dashboard/redesign/snapshot.js")
    assert r.status_code == 200
    assert "SNAPSHOT" in r.text
