"""Every governance-data GET route is auth-gated, and stays that way.

Motivation: /ws/eisv was once the only route on the server with no auth check
at all — over a tunnel `GET /v1/residents` answered 401 while the WebSocket
handshake answered 101 and streamed the full governance feed to any caller
(src/http_routes/access.py documents that incident). The WebSocket was closed;
its HTTP polling equivalents (/v1/eisv/latest, /v1/eisv/recent) and the
check-in bucket feed (/api/activity) were not, and stayed open until 2026-08-28.

This file is a REGISTRY-DRIVEN guard rather than a list of hand-written cases:
a new GET route added without a gate fails `test_no_ungated_governance_routes`
without anyone remembering to add a test for it. The public allowlist is
explicit and small, so making a route public is a visible, reviewable edit.
"""

from __future__ import annotations

import inspect
import re

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src import http_api
from src.http_routes import overview, telemetry


# Routes that answer without credentials BY DESIGN.
#
#   * liveness/readiness probes — monitored by things that hold no token, and
#     they carry no governance data (/health also advertises version+build_sha,
#     which is deliberate: `unitares status` reads it to report currency).
#   * the dashboard shell — static HTML/CSS/JS only. The page authenticates its
#     own data calls (dashboard/redesign/data.js: authFetch); gating the shell
#     would lock out the sign-in page that obtains the session in the first
#     place.
PUBLIC_BY_DESIGN = {
    "/",
    "/dashboard",
    "/dashboard/classic",
    "/dashboard/redesign",
    "/dashboard/redesign/{file:path}",
    "/dashboard/{file}",
    "/phase",
    "/health",
    "/health/live",
    "/health/ready",
    #   * the sign-in page — requiring a session to reach the page that
    #     establishes a session is a bootstrap deadlock. Its own docstring:
    #     "public, but inert until a credential is enrolled".
    "/auth/signin",
    #   * the attestation JWKS — publishes PUBLIC verification keys
    #     (export_public_jwks). Verifiers need them without credentials;
    #     that is what makes them public keys.
    "/v1/lease-holder/keys",
    #   * the enrollment UI — same bootstrap shape as /auth/signin: GET serves
    #     the page, and only its POST (which mints a bootstrap code) is gated,
    #     via _operator_token_authorized. Listed for the GET.
    "/auth/enroll",
    #   * the Wave 3a liveness ping — its own docstring: "Bare liveness probe.
    #     No auth, no data payload." Returns {ok, protocol_version} so the BEAM
    #     Finch client can verify connectivity before sending bearer headers.
    #     Its six data-bearing siblings under the same prefix ARE gated
    #     (_auth_or_response, fail-closed without WAVE_3A_PROBE_TOKEN).
    "/v1/probe/health",
}

# Auth gates a handler may legitimately call. Several exist because the
# surfaces differ: REST data routes take a bearer or trusted network
# (_check_http_auth), while the dashboard session routes take a validated
# session (_session_gate). A handler naming any of these is treated as gated.
#
# Known limitation, stated rather than papered over: this is a source-text
# check, so it proves a gate is MENTIONED, not that it runs before every
# response path. It catches the failure that actually happened here — a route
# with no gate at all — and not a mis-ordered one. The per-route request tests
# below are what pin behavior for the routes this change closed.
GATE_CALLS = (
    "_check_http_auth",   # REST data routes: bearer or trusted network
    "_session_gate",      # dashboard session routes: validated session
    "_auth_or_response",  # Wave 3a probe: WAVE_3A_PROBE_TOKEN, fail-closed
    "_is_operator",       # Wave 3a admin: operator credential
)

# Modules that register GET routes on the production app. http_api.py is the
# main table, but two Wave 3a registrars mount their own routes later
# (src/services/mcp_transport_service.py calls both), and a guard that read
# only http_api.py could not see them — it would report full coverage while
# being blind to seven live routes.
ROUTE_REGISTRARS = (
    "src.http_api",
    "src.mcp_handlers.wave3a_probe",
    "src.mcp_handlers.wave3a_admin",
)


def _registered_get_routes() -> list[tuple[str, object]]:
    """(path, handler) for every GET route the server registers.

    Reads every registrar in ROUTE_REGISTRARS. Paths built from a module
    constant (f"{PROBE_PREFIX}/health") are resolved against that module's
    attributes so they report as the real path rather than the f-string.
    """
    import importlib

    out: list[tuple[str, object]] = []
    for mod_name in ROUTE_REGISTRARS:
        mod = importlib.import_module(mod_name)
        source = inspect.getsource(mod)
        out.extend(_get_routes_in(mod, source))
    return out


def _resolve_path(mod, raw: str) -> str:
    """Expand a single {CONSTANT} prefix from the module's namespace."""
    m = re.fullmatch(r"\{(\w+)\}(.*)", raw)
    if not m:
        return raw
    prefix = getattr(mod, m.group(1), None)
    return f"{prefix}{m.group(2)}" if isinstance(prefix, str) else raw


def _get_routes_in(mod, source: str) -> list[tuple[str, object]]:
    # Match ANY methods list containing GET, not just the single-element
    # ["GET"] form: /auth/enroll and /auth/sessions register as
    # methods=["GET", "POST"], and a regex pinned to ["GET"] could not see
    # them — a guard with a blind spot is worse than no guard, because it
    # reads as coverage.
    pairs = []
    for path, fn_name, methods in re.findall(
        r'Route\(\s*f?"([^"]+)"\s*,\s*(\w+)\s*,\s*methods=(\[[^\]]*\])',
        source,
        re.S,
    ):
        if '"GET"' in methods:
            pairs.append((_resolve_path(mod, path), fn_name))
    out = []
    for path, fn_name in pairs:
        fn = getattr(mod, fn_name, None)
        if fn is not None:
            out.append((path, fn))
    return out


# Exact, not a floor. `> 20` against a real 54 would let the discovery regex
# lose half its matches and still report success — the guard would quietly stop
# guarding. When this number changes, that is a route added or removed: confirm
# the new one is gated or belongs in PUBLIC_BY_DESIGN, then update the count.
# 54 -> 51: the research-run registry retirement removed three GET routes
# (/v1/research/runs, /v1/research/runs/{run_id}, /v1/research/stats). No
# allowlist entry was left behind — they were credential-gated, not in
# PUBLIC_BY_DESIGN.
EXPECTED_GET_ROUTES = 51


def test_route_registry_is_readable():
    """Guard the guard: silent under-discovery is the failure mode that matters."""
    routes = _registered_get_routes()
    assert len(routes) == EXPECTED_GET_ROUTES, (
        f"discovered {len(routes)} GET routes, expected {EXPECTED_GET_ROUTES}. "
        "A route was added/removed (gate it or allowlist it, then bump the count), "
        "or the discovery regex stopped matching a registration shape."
    )


def test_no_ungated_governance_routes():
    """Every GET route calls the auth gate, or is explicitly public.

    Fails when a new route is added without either — which is the point.
    """
    ungated = []
    for path, fn in _registered_get_routes():
        if path in PUBLIC_BY_DESIGN:
            continue
        try:
            body = inspect.getsource(fn)
        except (OSError, TypeError):  # pragma: no cover - defensive
            continue
        if not any(gate in body for gate in GATE_CALLS):
            ungated.append(f"{path} -> {fn.__name__}")
    assert not ungated, (
        "GET routes with no auth gate and not in PUBLIC_BY_DESIGN:\n  "
        + "\n  ".join(ungated)
        + "\nGate it like its siblings, or add it to PUBLIC_BY_DESIGN with a reason."
    )


def test_public_allowlist_has_no_stale_entries():
    """An allowlist entry for a route that no longer exists hides the next one."""
    registered = {path for path, _ in _registered_get_routes()}
    stale = sorted(PUBLIC_BY_DESIGN - registered)
    assert not stale, f"PUBLIC_BY_DESIGN lists unregistered routes: {stale}"


# --- the three routes closed on 2026-08-28, pinned individually --------------

@pytest.fixture(autouse=True)
def _local_posture(monkeypatch):
    """Local posture with no token configured: the gate must fail closed."""
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


CLOSED_ROUTES = [
    ("/v1/eisv/latest", telemetry.http_eisv_latest),
    ("/v1/eisv/recent", telemetry.http_eisv_recent),
    ("/api/activity", overview.http_activity),
]


def test_multi_method_get_routes_are_discovered():
    """The registry pass must see routes registered with several verbs.

    /auth/enroll and /auth/sessions are methods=["GET", "POST"]. An earlier
    version of the regex required the single-element ["GET"] form and could
    not see them at all — so they were neither checked nor allowlisted, and a
    future ungated multi-method GET route would have passed silently.
    """
    paths = {path for path, _ in _registered_get_routes()}
    for path in ("/auth/enroll", "/auth/sessions"):
        assert path in paths, f"{path} (methods=[GET, POST]) not discovered"


def _client(path, handler, peer):
    app = Starlette(routes=[Route(path, handler, methods=["GET"])])
    return TestClient(app, client=peer)


@pytest.mark.parametrize(("path", "handler"), CLOSED_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_untrusted_peer_is_rejected(path, handler):
    """A public-internet peer gets 401, and no telemetry rides along.

    Asserting the BODY as well as the status is deliberate: a status-only
    check would still pass if the gate were moved below the data read, which
    is the shape a careless refactor produces. Seed the broadcaster first so
    there is real data to leak if the gate is bypassed.
    """
    from src import http_api as _api

    seeded = {
        "type": "eisv_update",
        "agent_id": "canary-agent-id",
        "eisv": {"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.5},
        "decision": {"action": "proceed"},
    }
    _api.broadcaster_instance.event_history.clear()
    _api.broadcaster_instance.event_history.append(seeded)
    _api.broadcaster_instance.last_update = seeded

    r = _client(path, handler, ("203.0.113.7", 44444)).get(path)
    assert r.status_code == 401, f"{path} answered {r.status_code} to an untrusted peer"
    for leak in ("canary-agent-id", "eisv", "proceed", "buckets"):
        assert leak not in r.text, f"{path} leaked {leak!r} in its 401 body"


@pytest.mark.parametrize(("path", "handler"), CLOSED_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_loopback_peer_still_served(path, handler):
    """The local operator keeps working: loopback is a trusted network."""
    r = _client(path, handler, ("127.0.0.1", 50000)).get(path)
    assert r.status_code == 200, f"{path} broke the local caller ({r.status_code})"


@pytest.mark.parametrize(("path", "handler"), CLOSED_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_bearer_token_is_accepted(path, handler, monkeypatch):
    """A tunnel caller with the configured token is served."""
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "test-token")
    r = _client(path, handler, ("203.0.113.7", 44444)).get(
        path, headers={"Authorization": "Bearer test-token"}
    )
    assert r.status_code == 200, f"{path} rejected a valid bearer ({r.status_code})"


@pytest.mark.parametrize(("path", "handler"), CLOSED_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_hosted_posture_has_no_network_bypass(path, handler, monkeypatch):
    """Hosted mode: an RFC1918 peer (cloud proxy) without a bearer is rejected.

    This is the branch the trusted-network bypass would otherwise open on a
    hosted deployment, where the proxy's source IP is typically 10.x.
    """
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "hosted-secret")
    r = _client(path, handler, ("10.1.2.3", 44444)).get(path)
    assert r.status_code == 401, f"{path} let a 10.x peer through in hosted mode"
