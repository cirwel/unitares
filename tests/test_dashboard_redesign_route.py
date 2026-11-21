"""Covers the additive /dashboard/redesign preview route.

The redesign reference is served from dashboard/redesign/** so it renders on
live governance data without touching the production /dashboard route. These
tests exercise the handler directly: it serves the entry page and nested
assets, and it stays sandboxed (no traversal, no disallowed file types).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.http_api import http_dashboard_redesign


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    # src/agent_metadata_model.py loads the repo's .env at import, so a local
    # checkout whose .env sets UNITARES_HTTP_API_TOKEN sends the entry page
    # into the token-inject auth check, which needs request.client that _req()
    # does not have. CI has no .env. The inject branch is covered with a real
    # peer in tests/test_dashboard_token_injection.py.
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


def _req(file: str):
    return SimpleNamespace(path_params={"file": file})


@pytest.mark.asyncio
async def test_serves_entry_page_by_default():
    resp = await http_dashboard_redesign(_req(""))
    assert resp.status_code == 200
    assert resp.media_type == "text/html"
    assert b"UNITARES" in resp.body
    # Base must be pinned so relative assets resolve regardless of trailing slash.
    assert b'<base href="/dashboard/redesign/">' in resp.body


@pytest.mark.asyncio
async def test_serves_nested_section_module():
    resp = await http_dashboard_redesign(_req("sections/landing.js"))
    assert resp.status_code == 200
    assert resp.media_type == "application/javascript"
    assert b"Landing" in resp.body


@pytest.mark.asyncio
async def test_serves_tokens_css():
    resp = await http_dashboard_redesign(_req("tokens.css"))
    assert resp.status_code == 200
    assert resp.media_type == "text/css"


@pytest.mark.asyncio
async def test_rejects_path_traversal():
    resp = await http_dashboard_redesign(_req("../http_api.py"))
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rejects_disallowed_extension():
    resp = await http_dashboard_redesign(_req("shot-eisv.png"))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_missing_file_is_404():
    resp = await http_dashboard_redesign(_req("does-not-exist.js"))
    assert resp.status_code == 404


def _assert_security_headers(headers):
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("referrer-policy") == "no-referrer"
    assert headers.get("x-frame-options") == "DENY"
    csp = headers.get("content-security-policy", "")
    for directive in (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ):
        assert directive in csp, f"missing CSP directive {directive!r} in {csp!r}"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/dashboard",
        "/dashboard/redesign",
        "/dashboard/redesign/",
        "/dashboard/redesign/app.html",
        "/dashboard/redesign/tokens.css",
        "/dashboard/redesign/sections/landing.js",
        "/dashboard/phase.js",
        "/phase",
    ],
)
def test_dashboard_routes_send_baseline_security_headers(path):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from src.http_routes.dashboard import (
        http_dashboard_classic_redirect,
        http_dashboard_static,
        http_phase,
    )

    app = Starlette(
        routes=[
            Route("/dashboard/redesign", http_dashboard_redesign, methods=["GET"]),
            Route("/dashboard/redesign/{file:path}", http_dashboard_redesign, methods=["GET"]),
            Route("/dashboard/classic", http_dashboard_classic_redirect, methods=["GET"]),
            Route("/dashboard/{file}", http_dashboard_static, methods=["GET"]),
            Route("/dashboard", http_dashboard_redesign, methods=["GET"]),
            Route("/phase", http_phase, methods=["GET"]),
            Route("/", http_dashboard_redesign, methods=["GET"]),
        ]
    )
    resp = TestClient(app, client=("127.0.0.1", 50000)).get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    _assert_security_headers(resp.headers)


def test_dashboard_classic_redirect_sends_baseline_security_headers():
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from src.http_routes.dashboard import http_dashboard_classic_redirect

    app = Starlette(
        routes=[Route("/dashboard/classic", http_dashboard_classic_redirect, methods=["GET"])]
    )
    resp = TestClient(app, client=("127.0.0.1", 50000)).get(
        "/dashboard/classic", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"
    _assert_security_headers(resp.headers)


@pytest.mark.parametrize(
    "path, peer, expected_status",
    [
        ("/dashboard/redesign/does-not-exist.js", ("127.0.0.1", 50000), 404),
        ("/dashboard/redesign/auth/signin.html", ("127.0.0.1", 50000), 404),
        ("/dashboard/redesign/shot-eisv.png", ("127.0.0.1", 50000), 403),
        ("/dashboard/redesign/snapshot.js", ("203.0.113.7", 44444), 401),
        ("/dashboard/not-allowed.js", ("127.0.0.1", 50000), 403),
    ],
)
def test_dashboard_error_responses_send_baseline_security_headers(path, peer, expected_status):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from src.http_routes.dashboard import http_dashboard_static

    app = Starlette(
        routes=[
            Route("/dashboard/redesign/{file:path}", http_dashboard_redesign, methods=["GET"]),
            Route("/dashboard/{file}", http_dashboard_static, methods=["GET"]),
        ]
    )
    resp = TestClient(app, client=peer).get(path)
    assert resp.status_code == expected_status, f"{path} -> {resp.status_code}"
    _assert_security_headers(resp.headers)


@pytest.mark.asyncio
async def test_dashboard_bad_request_sends_baseline_security_headers():
    resp = await http_dashboard_redesign(_req("../http_api.py"))
    assert resp.status_code == 400
    _assert_security_headers(resp.headers)
