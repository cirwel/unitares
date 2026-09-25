"""Google's custom MCP connector could not link ("Account linking is
required"). The SDK requires PKCE on every flow and refuses unknown scopes,
and nothing recorded why a sign-in failed. These pin the static-client
compat (PKCE optional for the confidential client, scopes narrowed) and the
attempt log that makes the next failure visible.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.oauth_provider import (
    GovernanceOAuthProvider,
    OAuthAttemptLogger,
    StaticClientBasicAuthShim,
    build_static_client,
    static_pkce_verifier,
)

CID, SECRET = "google", "s3cret"
REDIRECT = "https://oauth-redirect.googleusercontent.com/r/example"


def _app(*, compat=True, dcr=False):
    provider = GovernanceOAuthProvider(static_clients=[build_static_client(CID, SECRET, [REDIRECT])])
    app = Starlette(routes=create_auth_routes(
        provider,
        issuer_url=AnyHttpUrl("https://gov.example.org"),
        client_registration_options=ClientRegistrationOptions(
            enabled=dcr, valid_scopes=["mcp:tools"], default_scopes=["mcp:tools"]
        ),
    ))
    app.add_middleware(
        StaticClientBasicAuthShim,
        client_id=CID,
        pkce_verifier=static_pkce_verifier(SECRET) if compat else None,
    )
    app.add_middleware(OAuthAttemptLogger)
    return TestClient(app)


def _authorize(client, **params):
    base = {"response_type": "code", "client_id": CID, "redirect_uri": REDIRECT, "state": "s"}
    base.update(params)
    resp = client.get("/authorize", params={k: v for k, v in base.items() if v is not None},
                      follow_redirects=False)
    assert resp.status_code == 302, resp.text
    return parse_qs(urlparse(resp.headers["location"]).query)


def _token(client, code, **extra):
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": CID, "client_secret": SECRET}
    data.update(extra)
    return client.post("/token", data={k: v for k, v in data.items() if v is not None})


def test_static_client_links_without_pkce():
    client = _app()
    q = _authorize(client)
    assert "code" in q, q
    resp = _token(client, q["code"][0])
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"].startswith("at_")


def test_without_compat_a_pkce_less_request_is_refused():
    """What Google hit before: the SDK requires code_challenge."""
    q = _authorize(_app(compat=False))
    assert q.get("error") == ["invalid_request"]


def test_a_client_sending_pkce_is_never_altered():
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    client = _app()
    q = _authorize(client, code_challenge=challenge, code_challenge_method="S256")
    # its own verifier works
    assert _token(client, q["code"][0], code_verifier=verifier).status_code == 200
    # and the server-held verifier would not have
    q2 = _authorize(client, code_challenge=challenge, code_challenge_method="S256")
    assert _token(client, q2["code"][0], code_verifier=static_pkce_verifier(SECRET)).status_code == 400


def test_compat_still_requires_the_client_secret():
    client = _app()
    q = _authorize(client)
    assert _token(client, q["code"][0], client_secret="wrong").status_code == 401


@pytest.mark.parametrize("requested", ["openid", "openid email", "mcp:tools openid", "", None])
def test_unknown_scopes_are_narrowed_not_refused(requested):
    client = _app()
    q = _authorize(client, scope=requested)
    assert "code" in q, q
    resp = _token(client, q["code"][0])
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "mcp:tools"


def test_other_clients_get_no_compat():
    """A DCR client without PKCE is still refused."""
    client = _app(dcr=True)
    reg = client.post("/register", json={"redirect_uris": ["https://x.example/cb"]}).json()
    resp = client.get("/authorize", params={
        "response_type": "code", "client_id": reg["client_id"],
        "redirect_uri": "https://x.example/cb", "state": "s",
    }, follow_redirects=False)
    assert parse_qs(urlparse(resp.headers["location"]).query).get("error") == ["invalid_request"]


def test_attempts_are_logged_without_secrets(caplog):
    client = _app(compat=False)
    with caplog.at_level(logging.INFO, logger="src.oauth_provider"):
        _authorize(client, scope="openid")
        _token(client, "bogus-code")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[OAUTH]")]
    assert any("authorize" in l and "pkce=no" in l and "scope=openid" in l
               and "error=invalid_request" in l for l in lines), lines
    assert any("token" in l and "grant=authorization_code" in l and "auth=post" in l
               and "-> 400" in l for l in lines), lines
    blob = "\n".join(lines)
    assert SECRET not in blob and "bogus-code" not in blob


def test_logged_basic_auth_names_the_client_not_the_secret(caplog):
    client = _app()
    q = _authorize(client)
    basic = "Basic " + base64.b64encode(f"{CID}:{SECRET}".encode()).decode()
    with caplog.at_level(logging.INFO, logger="src.oauth_provider"):
        resp = client.post("/token", data={"grant_type": "authorization_code",
                                           "code": q["code"][0], "redirect_uri": REDIRECT},
                           headers={"Authorization": basic})
    assert resp.status_code == 200, resp.text
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[OAUTH]")]
    assert any(f"client={CID}" in l and "auth=basic" in l and "-> 200" in l for l in lines), lines
    assert SECRET not in "\n".join(lines)


def test_verifier_is_stable_and_valid_pkce():
    v = static_pkce_verifier(SECRET)
    assert v == static_pkce_verifier(SECRET)
    assert v != static_pkce_verifier("other")
    assert 43 <= len(v) <= 128 and all(c.isalnum() or c in "-._~" for c in v)
