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

import src.oauth_provider as _op
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


@pytest.fixture(autouse=True)
def _fresh_log_budget(monkeypatch):
    monkeypatch.setattr(_op, "_LOG_BUDGET", _op._LineBudget())
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
    assert any("authorize" in l and 'pkce="no"' in l and 'scope="openid"' in l
               and 'error="invalid_request"' in l for l in lines), lines
    assert any("token" in l and 'grant="authorization_code"' in l and 'auth="post"' in l
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
    assert any(f'client="{CID}"' in l and 'auth="basic"' in l and "-> 200" in l for l in lines), lines
    assert SECRET not in "\n".join(lines)


def test_verifier_is_stable_and_valid_pkce():
    v = static_pkce_verifier(SECRET)
    assert v == static_pkce_verifier(SECRET)
    assert v != static_pkce_verifier("other")
    assert 43 <= len(v) <= 128 and all(c.isalnum() or c in "-._~" for c in v)


def test_scopes_are_narrowed_even_with_the_clients_own_pkce():
    verifier = "w" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    client = _app()
    q = _authorize(client, code_challenge=challenge, code_challenge_method="S256", scope="openid")
    assert "code" in q, q
    resp = _token(client, q["code"][0], code_verifier=verifier)
    assert resp.status_code == 200 and resp.json()["scope"] == "mcp:tools"


def test_a_newline_cannot_forge_an_oauth_log_line(caplog):
    client = _app(compat=False)
    forged = "x\n[OAUTH] token client=google auth=post grant=authorization_code -> 200"
    with caplog.at_level(logging.INFO, logger="src.oauth_provider"):
        _authorize(client, scope=forged)
        client.post("/token", data={"grant_type": "g" * 70000})
    msgs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[OAUTH]")]
    assert all("\n" not in m for m in msgs)
    assert all(len(m) < 2000 for m in msgs)


def test_an_echoed_redirect_uri_loses_its_query_in_the_log(caplog):
    client = _app(compat=False)
    with caplog.at_level(logging.INFO, logger="src.oauth_provider"):
        client.get("/authorize", params={
            "response_type": "code", "client_id": CID,
            "redirect_uri": "https://evil.example/cb?session=SECRETQUERY",
            "code_challenge": "c" * 43, "code_challenge_method": "S256",
        }, follow_redirects=False)
    ours = [r.getMessage() for r in caplog.records if r.name == "src.oauth_provider"]
    assert any(m.startswith("[OAUTH] authorize") for m in ours), ours
    assert "SECRETQUERY" not in "\n".join(ours)


@pytest.mark.asyncio
async def test_main_wires_the_static_pkce_verifier_into_the_runtime(monkeypatch):
    """The production path, not a hand-built app: without this wiring the
    shipped server refuses a PKCE-less sign-in again."""
    from types import SimpleNamespace

    from src import mcp_server

    seen = {}

    class _Stop(Exception):
        pass

    class _Bootstrap:
        async def shutdown(self):
            pass

    async def _bootstrap(**_kwargs):
        return _Bootstrap()

    def _build(*_a, auth_config, **_k):
        seen["auth"] = auth_config
        raise _Stop

    monkeypatch.setattr("src.services.mcp_server_bootstrap.bootstrap_server", _bootstrap)
    monkeypatch.setattr("src.services.mcp_transport_service.build_transport_runtime", _build)
    monkeypatch.setattr(mcp_server, "_OAUTH_GATE_REQUIRED", False)
    monkeypatch.setattr(mcp_server, "_oauth_public_port", None)
    monkeypatch.setattr(mcp_server, "_oauth_provider", object())
    monkeypatch.setattr(mcp_server, "_oauth_issuer_url", "https://gov.example.org")
    monkeypatch.setattr(mcp_server, "_oauth_static_client_id", CID)
    monkeypatch.setattr(mcp_server, "_static_pkce_verifier", static_pkce_verifier(SECRET))
    monkeypatch.setattr(
        mcp_server, "parse_args",
        lambda: SimpleNamespace(host="127.0.0.1", port=8767, force=False, reload=False),
    )
    with pytest.raises(SystemExit):
        await mcp_server.main()
    assert seen["auth"].static_pkce_verifier == static_pkce_verifier(SECRET)
    assert seen["auth"].static_client_id == CID


def test_import_derives_the_verifier_from_the_static_client_env(tmp_path):
    """Module scope: static_clients_from_env -> static_pkce_verifier."""
    import os
    import subprocess
    import sys

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        **os.environ,
        "PYTHONPATH": repo,
        "UNITARES_OAUTH_ISSUER_URL": "https://gov.example.org",
        "UNITARES_OAUTH_STATIC_CLIENT_ID": CID,
        "UNITARES_OAUTH_STATIC_CLIENT_SECRET": SECRET,
        "UNITARES_OAUTH_STATIC_REDIRECT_URIS": REDIRECT,
    }
    for k in ("UNITARES_OAUTH_PUBLIC_PORT", "UNITARES_OAUTH_REQUIRED"):
        env.pop(k, None)
    code = (
        "from src import mcp_server as m\n"
        "from src.oauth_provider import static_pkce_verifier as v\n"
        f"print(m._static_pkce_verifier == v({SECRET!r}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "True", out.stderr[-2000:]


def test_refresh_resending_a_foreign_scope_still_works():
    """Linked once with scope=openid; a connector that re-sends it on refresh
    must not lose the session an hour later."""
    client = _app()
    q = _authorize(client, scope="openid")
    tokens = _token(client, q["code"][0]).json()
    resp = client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "scope": "openid", "client_id": CID, "client_secret": SECRET,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "mcp:tools"


@pytest.mark.parametrize("method", ["", "S256"])
def test_a_blank_code_challenge_counts_as_no_pkce(method):
    client = _app()
    q = _authorize(client, code_challenge="", code_challenge_method=method)
    assert "code" in q, q
    assert _token(client, q["code"][0]).status_code == 200


def test_a_logged_value_cannot_fake_another_field(caplog):
    client = _app(compat=False)
    with caplog.at_level(logging.INFO, logger="src.oauth_provider"):
        client.get("/authorize", params={
            "response_type": "code", "client_id": "bogus auth=basic -> 200",
            "redirect_uri": REDIRECT, "scope": "x pkce=yes -> 200",
        }, follow_redirects=False)
    line = next(r.getMessage() for r in caplog.records
                if r.name == "src.oauth_provider" and r.getMessage().startswith("[OAUTH]"))
    assert 'client="bogus auth=basic -> 200"' in line
    assert 'scope="x pkce=yes -> 200"' in line
    assert 'pkce="no"' in line


@pytest.mark.parametrize("bad", ["http://[::1", "http://[zz]/cb", "https://%zz"])
def test_the_logger_never_changes_a_response(bad):
    """A malformed redirect_uri must get the SDK's own 400, not a 500."""
    with_log = _app(compat=False)
    plain = Starlette(routes=create_auth_routes(
        GovernanceOAuthProvider(static_clients=[build_static_client(CID, SECRET, [REDIRECT])]),
        issuer_url=AnyHttpUrl("https://gov.example.org"),
    ))
    params = {"response_type": "code", "client_id": CID, "redirect_uri": bad,
              "code_challenge": "c" * 43, "code_challenge_method": "S256"}
    a = with_log.get("/authorize", params=params, follow_redirects=False)
    b = TestClient(plain).get("/authorize", params=params, follow_redirects=False)
    assert a.status_code == b.status_code != 500


def test_userinfo_and_resource_queries_stay_out_of_the_log(caplog):
    client = _app(compat=False)
    with caplog.at_level(logging.INFO, logger="src.oauth_provider"):
        client.get("/authorize", params={
            "response_type": "code", "client_id": CID,
            "redirect_uri": "https://user:hunter2@evil.example/cb",
            "resource": "https://gov.example.org/mcp?token=RESOURCESECRET",
            "code_challenge": "c" * 43, "code_challenge_method": "S256",
        }, follow_redirects=False)
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name == "src.oauth_provider")
    assert "[OAUTH] authorize" in ours
    assert "hunter2" not in ours and "RESOURCESECRET" not in ours
    assert 'redirect_host="evil.example"' in ours


def _dcr_client(client):
    return client.post("/register", json={"redirect_uris": ["https://x.example/cb"]}).json()


def test_other_clients_get_no_compat_on_form_body_paths():
    """POST /authorize and /token rewrites are the static client's alone."""
    client = _app(dcr=True)
    reg = _dcr_client(client)
    resp = client.post("/authorize", data={
        "response_type": "code", "client_id": reg["client_id"],
        "redirect_uri": "https://x.example/cb", "state": "s",
    }, follow_redirects=False)
    q = parse_qs(urlparse(resp.headers["location"]).query)
    assert q.get("error") == ["invalid_request"], q


def test_other_clients_refresh_scope_is_not_narrowed():
    client = _app(dcr=True)
    reg = _dcr_client(client)
    verifier = "d" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    resp = client.get("/authorize", params={
        "response_type": "code", "client_id": reg["client_id"],
        "redirect_uri": "https://x.example/cb", "code_challenge": challenge,
        "code_challenge_method": "S256",
    }, follow_redirects=False)
    code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
    tokens = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": "https://x.example/cb",
        "code_verifier": verifier, "client_id": reg["client_id"], "client_secret": reg["client_secret"],
    }).json()
    resp = client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "scope": "openid",
        "client_id": reg["client_id"], "client_secret": reg["client_secret"],
    })
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_scope"


def test_logged_fields_are_capped(caplog):
    client = _app(compat=False)
    with caplog.at_level(logging.INFO, logger="src.oauth_provider"):
        client.get("/authorize", params={
            "response_type": "code", "client_id": CID, "redirect_uri": REDIRECT,
            "scope": "s" * 5000,
        }, follow_redirects=False)
    line = next(r.getMessage() for r in caplog.records
                if r.name == "src.oauth_provider" and r.getMessage().startswith("[OAUTH]"))
    assert "s" * 200 not in line and len(line) < 1500


def test_a_blank_code_verifier_counts_as_absent():
    client = _app()
    q = _authorize(client)
    assert _token(client, q["code"][0], code_verifier="").status_code == 200


def test_a_failure_describing_the_request_never_changes_the_response(monkeypatch, caplog):
    import src.oauth_provider as op

    def _boom(*_a, **_k):
        raise RuntimeError("describe failed")

    monkeypatch.setattr(op, "_attempt_facts", _boom)
    client = _app()
    with caplog.at_level(logging.INFO, logger="src.oauth_provider"):
        q = _authorize(client)
    assert "code" in q, q
    assert any("unavailable (RuntimeError)" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_disconnect_mid_body_passes_through_unchanged():
    """The app sees the same stream it would without the logger."""
    seen = []

    async def inner(scope, receive, send):
        while True:
            message = await receive()
            seen.append(message["type"])
            if message["type"] != "http.request" or not message.get("more_body"):
                break

    stream = [{"type": "http.request", "body": b"grant_type=", "more_body": True},
              {"type": "http.disconnect"}]

    async def receive():
        return stream.pop(0)

    async def send(_message):
        pass

    scope = {"type": "http", "method": "POST", "path": "/token", "headers": [], "query_string": b""}
    await OAuthAttemptLogger(inner)(scope, receive, send)
    assert seen == ["http.request", "http.disconnect"]


@pytest.mark.parametrize("grant", ["authorization_code", "refresh_token"])
def test_an_oversized_body_gets_the_sdks_answer_not_the_loggers(grant):
    """The logger reads a prefix only; the SDK still answers (its limit is
    4 MiB), so a 100 KB body is not turned into a 413."""
    plain = TestClient(Starlette(routes=create_auth_routes(
        GovernanceOAuthProvider(static_clients=[build_static_client(CID, SECRET, [REDIRECT])]),
        issuer_url=AnyHttpUrl("https://gov.example.org"),
    )))
    padded = {"grant_type": grant, "client_id": CID, "client_secret": SECRET,
              "code": "x", "refresh_token": "x", "pad": "p" * 100_000}
    a = _app(compat=False).post("/token", data=padded)
    b = plain.post("/token", data=padded)
    assert a.status_code == b.status_code != 413


def test_log_lines_are_rate_limited_and_drops_counted(monkeypatch, caplog):
    monkeypatch.setattr(_op, "_LOG_BUDGET", _op._LineBudget(per_window=3, window=3600))
    client = _app(compat=False)
    with caplog.at_level(logging.INFO, logger="src.oauth_provider"):
        for _ in range(6):
            client.get("/authorize", params={"response_type": "code", "client_id": CID,
                                             "redirect_uri": REDIRECT}, follow_redirects=False)
    lines = [r.getMessage() for r in caplog.records if r.name == "src.oauth_provider"]
    assert sum(1 for l in lines if l.startswith("[OAUTH] authorize")) == 3


def test_line_budget_reports_drops_on_the_next_logged_line():
    budget = _op._LineBudget(per_window=1, window=0.0)
    assert budget.take() == 0
    budget._window = 3600
    assert budget.take() is None and budget.take() is None
    budget._window = 0.0
    assert budget.take() == 2
