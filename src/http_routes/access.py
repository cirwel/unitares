"""Request interpretation shared by every HTTP route: trusted-network and
bearer auth checks, session-signal extraction, sticky operator binding, and
query-param coercion. Route modules call these through the module object
(``access._check_http_auth(...)``) so tests can patch one path.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import asyncio
import ipaddress as _ipaddress
import secrets

from starlette.responses import JSONResponse


from src.logging_utils import get_logger
from src.mcp_listen_config import (
    check_mcp_bearer,
    mcp_bearer_required,
    rest_strict_required,
)
from src.dashboard_auth import (
    DASHBOARD_EXPECTED_ORIGIN,
    dashboard_session_authenticated,
)

logger = get_logger(__name__)

# Bound for the session-identity lookup `_explicit_bind_corroboration` makes
# to verify csid ownership. Same order of magnitude as this codebase's other
# Redis-guard budgets (identity_step.py's 500ms) — degrade to "no
# corroboration" on timeout rather than stall a REST call.
_CORROBORATION_LOOKUP_TIMEOUT = 0.5


def _build_http_session_signals(request):
    """Build SessionSignals from an HTTP request.

    The optional ``unitares_peer_pid`` scope key is server-injected by the
    owner-only UDS listener after a kernel ``LOCAL_PEERPID`` lookup.  Preserve
    that signal for the direct REST tool bridge: substrate-attested residents
    use ``POST /v1/tools/call``, not the streamable ``/mcp`` route, so dropping
    it here silently turns an authenticated UDS request back into plain HTTP.

    Only a positive, non-bool integer is accepted.  Request headers never
    participate in this decision; ordinary TCP HTTP remains ``rest``.
    """
    from src.mcp_handlers.context import SessionSignals, detect_client_from_user_agent
    from src.model_harness_provenance import runtime_signal_fields_from_headers

    ua = request.headers.get("user-agent", "")
    runtime_fields = runtime_signal_fields_from_headers(request.headers)
    x_session_id = request.headers.get("X-Session-ID") or request.headers.get("x-session-id")
    peer_pid = getattr(request, "scope", {}).get("unitares_peer_pid")
    if isinstance(peer_pid, bool) or not isinstance(peer_pid, int) or peer_pid <= 0:
        peer_pid = None

    ip_ua_fp = None
    try:
        host = request.client.host if request.client else "unknown"
        import hashlib
        ua_fp = hashlib.md5(ua.encode()).hexdigest()[:6] if ua else "000000"
        from src.mcp_handlers.context import note_ua_fingerprint
        note_ua_fingerprint(ua_fp, ua)
        ip_ua_fp = f"{host}:{ua_fp}"
    except Exception:
        pass

    return SessionSignals(
        x_session_id=x_session_id,
        x_client_id=request.headers.get("x-client-id") or request.headers.get("x-mcp-client-id"),
        ip_ua_fingerprint=ip_ua_fp,
        user_agent=ua,
        client_hint=detect_client_from_user_agent(ua),
        **runtime_fields,
        x_agent_name=request.headers.get("x-agent-name"),
        x_agent_id=request.headers.get("x-agent-id"),
        transport="uds" if peer_pid is not None else "rest",
        peer_pid=peer_pid,
        unitares_operator_token=request.headers.get("x-unitares-operator"),
    )

# ---------------------------------------------------------------------------
# Trusted networks: localhost, Tailscale CGNAT, private RFC1918 ranges
# ---------------------------------------------------------------------------
_TRUSTED_NETWORKS = [
    _ipaddress.ip_network("127.0.0.0/8"),
    _ipaddress.ip_network("::1/128"),
    _ipaddress.ip_network("100.64.0.0/10"),   # Tailscale CGNAT
    _ipaddress.ip_network("192.168.0.0/16"),
    _ipaddress.ip_network("10.0.0.0/8"),
    _ipaddress.ip_network("172.16.0.0/12"),
]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _is_trusted_network(request) -> bool:
    """Check if request originates from a trusted network.

    Uses the actual TCP peer address only -- never trust X-Forwarded-For
    since there is no reverse proxy stripping it before us.
    """
    client_ip = request.client.host if request.client else None
    if not client_ip:
        return False
    try:
        addr = _ipaddress.ip_address(client_ip)
        return any(addr in net for net in _TRUSTED_NETWORKS)
    except ValueError:
        return False


def _http_unauthorized():
    return JSONResponse(
        {
            "success": False,
            "error": "Unauthorized",
            "hint": "Sign in at /auth/signin or provide a valid bearer token. Tokens come from UNITARES_MCP_BEARER_TOKENS (hosted) or UNITARES_HTTP_API_TOKEN (local).",
        },
        status_code=401,
    )


def _bearer_from_header(auth: str | None) -> str | None:
    """Extract ``<tok>`` from an ``Authorization: Bearer <tok>`` header."""
    if not auth or not isinstance(auth, str):
        return None
    if not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip()


def _check_ws_auth(websocket, *, http_api_token: str | None) -> bool:
    """Bearer token auth for WebSocket endpoints.

    Same posture as :func:`_check_http_auth` — hosted mode (``UNITARES_MCP_BEARER_TOKENS``)
    requires a valid bearer with no IP bypass; the legacy/local posture keeps the
    trusted-network bypass and then gates on ``UNITARES_HTTP_API_TOKEN``.

    A browser cannot set request headers on a ``WebSocket``, so the break-glass
    bearer rides in the query string (``/ws/eisv?token=…``); non-browser clients
    may still send the ``Authorization`` header. A DB-validated passkey session
    is also accepted in local posture when the browser supplies the exact RP
    Origin.

    Without this, ``/ws/eisv`` was the only route on the server with no auth
    check at all: over the tunnel ``GET /v1/residents`` answered 401 while the
    WebSocket handshake answered 101 and streamed the full governance feed
    (agent ids, EISV, risk, verdicts, and Lumen's raw sensor payload) to any
    unauthenticated caller.
    """
    tok = websocket.query_params.get("token") or _bearer_from_header(
        websocket.headers.get("authorization") or websocket.headers.get("Authorization")
    )

    # Strict posture: bearer, or a validated session from our exact RP origin.
    #
    # Same operator decision as _check_http_auth, with the Origin pin the
    # local branch below already applies: WebSockets receive no CORS
    # protection and sibling subdomains are same-site, so a cookie alone is
    # not sufficient evidence on this transport.
    if rest_strict_required():
        # Guarded for the same reason as _check_http_auth: an unconfigured
        # allowlist makes check_mcp_bearer answer True for everyone.
        if mcp_bearer_required() and check_mcp_bearer(f"Bearer {tok}" if tok else None):
            return True
        if dashboard_session_authenticated(websocket):
            origin = websocket.headers.get("origin") or websocket.headers.get("Origin")
            return bool(origin) and secrets.compare_digest(
                origin, DASHBOARD_EXPECTED_ORIGIN
            )
        return False

    # Legacy / local posture. Keep explicit tokens as break-glass, then allow
    # a DB-validated cookie only from our exact RP origin (WebSockets do not
    # receive CORS protection and sibling subdomains are same-site).
    if tok and http_api_token and secrets.compare_digest(tok, http_api_token):
        return True
    if dashboard_session_authenticated(websocket):
        origin = websocket.headers.get("origin") or websocket.headers.get("Origin")
        return bool(origin) and secrets.compare_digest(origin, DASHBOARD_EXPECTED_ORIGIN)

    # Loopback and Tailscale stay unauthenticated in local posture.
    if _is_trusted_network(websocket):
        return True
    # An unset local token is deny, not "gate disabled". Passkey sessions and
    # trusted-network access above remain usable after deliberate token removal.
    return False


def _check_http_auth(request, *, http_api_token: str | None) -> bool:
    """Bearer token auth for HTTP endpoints.

    Strict mode — selected by ``UNITARES_REST_STRICT``, which defaults to
    whether ``UNITARES_MCP_BEARER_TOKENS`` is configured: a valid MCP bearer or
    a DB-validated dashboard session is required and the trusted-network bypass
    does **not** apply. This closes a real gap: the
    trusted set includes every RFC1918 range (10/8, 192.168/16, 172.16/12) plus
    Tailscale, so a hosted server behind a cloud proxy (source IP typically
    ``10.x``) would otherwise bypass auth on the write path. Same token, same
    rule, both transports.

    Local / self-host default — no MCP bearer configured: trusted networks
    bypass, while the rest require either ``UNITARES_HTTP_API_TOKEN`` or a
    DB-validated passkey session. An unset local token fails closed.
    """
    # Strict posture: a bearer or a validated passkey session; no IP bypass.
    #
    # The session is admitted here by operator decision (2026-08-28). It is a
    # DB-validated, revocable credential — strictly stronger evidence than the
    # source-IP check that satisfies local posture — and without it strict
    # posture has no browser story at all: a navigation cannot set an
    # Authorization header, so the dashboard was unusable by anyone, signed in
    # or not. The trusted-network bypass stays off, which is the gap this
    # branch exists to close (a hosted proxy's source IP is typically 10.x).
    # mcp_bearer_required() guards the bearer check because check_mcp_bearer
    # returns True when NO allowlist is configured ("gate off") — correct for
    # the /mcp gate it was written for, catastrophic here: strict posture with
    # no bearer set would authenticate every caller. Before these two
    # predicates were separable that state was unreachable; now it is one
    # UNITARES_REST_STRICT=1 away, so it is checked explicitly.
    if rest_strict_required():
        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if mcp_bearer_required() and check_mcp_bearer(auth):
            return True
        return dashboard_session_authenticated(request)

    # Legacy / local posture: trusted network -> bearer -> validated session.
    if _is_trusted_network(request):
        return True
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    token = _bearer_from_header(auth)
    if token and http_api_token and secrets.compare_digest(token, http_api_token):
        return True
    if dashboard_session_authenticated(request):
        return True
    # Fail closed when UNITARES_HTTP_API_TOKEN is unset. Removing the token is
    # a deliberate lockout of non-session, non-trusted clients—not cleanup.
    return False


async def _extract_client_session_id(request) -> tuple[str, bool]:
    """
    Stable per-client session id for HTTP callers, plus whether the CALLER
    actually asserted it.
    Uses SessionSignals + derive_session_key() for unified derivation.
    Falls back to legacy logic if signals unavailable.

    Returns ``(client_session_id, is_caller_asserted)``.

    ``is_caller_asserted`` is read from `get_session_proof_origin()`, not
    re-derived here — `derive_session_key` -> `_mark()`
    (`identity/session.py`) already single-sources this classification
    across its full priority ladder, and an earlier version of this
    function that approximated it locally (``result == ip_ua_fp and not
    x_session_id``) got it wrong for a real branch: an onboard-PIN hit
    (`_mark("pinned_onboard_session")`, step 7) resolves to a stable,
    non-fingerprint value with no ``x_session_id`` present, so the local
    approximation read it as caller-asserted — but `_mark()` itself
    classifies a pin hit as ``server_inferred`` (it is NOT in
    ``_CALLER_ASSERTED_SOURCES``): the pin is keyed on IP+UA fingerprint,
    which the docstring on `_extract_base_fingerprint` notes is shared
    across unrelated callers behind the same proxy pool or UA string, so
    treating a pin hit as proof would let one caller's identity claim ride
    another caller's recent onboard. Found via a codex review probing that
    exact case against this file's own test suite.
    """
    from src.mcp_handlers.context import get_session_proof_origin
    from src.mcp_handlers.identity.handlers import derive_session_key

    signals = _build_http_session_signals(request)
    x_session_id = signals.x_session_id
    ip_ua_fp = signals.ip_ua_fingerprint

    result = await derive_session_key(signals)
    is_caller_asserted = get_session_proof_origin() == "caller_asserted"

    # If derive_session_key returned the raw IP:UA fingerprint (no pin found),
    # and there's no explicit session header, generate a unique ID so REST
    # clients without session headers get distinct identities per request chain.
    if result == ip_ua_fp and not x_session_id:
        try:
            if hasattr(request, "state") and hasattr(request.state, "governance_client_id"):
                return str(getattr(request.state, "governance_client_id")), False
        except Exception:
            pass
        import uuid as _uuid
        unique_id = str(_uuid.uuid4())[:12]
        try:
            host = request.client.host if request.client else "unknown"
            return f"http:{host}:{unique_id}", False
        except Exception:
            return f"http:unknown:{unique_id}", False

    return result, is_caller_asserted


_HTTP_PREBIND_SKIP_TOOLS = {
    "identity",
    "onboard",
    "bind_session",
    "health_check",
    "list_tools",
    "get_server_info",
    "describe_tool",
    "debug_request_context",
}


async def _explicit_bind_corroboration(arguments: dict) -> str:
    """Classify what *else* a caller offered alongside a declared ``agent_id``.

    Exists so the gate can answer one question with data instead of
    estimation: if the explicit-``agent_id`` path required corroborating
    proof, which live callers would still bind?

      * ``csid``  — a caller-asserted ``client_session_id`` that resolves to
        the SAME ``agent_id`` being claimed.
      * ``token`` — ``continuity_token`` that verifies AND names this same
        ``agent_id`` — same-live-process rebind proof.
      * ``none``  — the uuid and nothing else.

    ``none`` is the population a corroboration requirement turns away, and
    therefore the whole cost of tightening this path.

    Both proof kinds need TWO checks, not one — proof of *something* is not
    proof of *this agent_id*:

    1. A present ``client_session_id`` is not by itself caller proof:
       `_inject_http_client_session` (http_routes/tools.py) synthesizes one
       into ``arguments`` for every REST call that doesn't already carry
       one, so presence alone is satisfied by every request regardless of
       what the caller actually sent — the identical argument-presence trap
       PR #608 and the strict-identity gate docstring both warn about ("DO
       NOT TRUST client_session_id FOR AUTH"). `get_csid_transport_injected()`
       is the server's own record of which case this request is; a
       transport-injected id is server-inferred and must not count.
    2. A caller-asserted ``client_session_id`` is STILL not proof of THIS
       claim: it proves the caller completed some onboard, not that the
       onboard was for the ``agent_id`` in this call. Found via a codex
       review that reproduced it directly: an attacker's own real session
       plus `agent_id=<victim uuid>` would otherwise corroborate — a
       confused-deputy bypass one level up from the token branch's, and
       just as real, since a caller always has cheap access to a real
       session of their own. `resolve_session_identity` (the codebase's
       single identity-resolution function, `persist=False` so this check
       creates nothing) must resolve the session to the SAME uuid.
    3. Equality with the claimed uuid is STILL not proof, for the canonical
       ``agent-{uuid[:12]}`` session-id form specifically: it is a pure,
       documented function of the public uuid alone (`make_client_session_id`,
       `identity/shared.py`) — the exact prefix-bind hijack a prior KG finding
       (2026-04-20) and #802 already named, mitigated elsewhere only by a
       fingerprint check this codebase deliberately defaults to `log`, not
       `strict` (co-resident localhost clients legitimately share IP+UA).
       A caller who merely KNOWS the uuid — the entire threat this gate
       exists to require more than — can compute this value with no
       session of their own, so it must never corroborate here regardless
       of what it resolves to. Caught by a third codex review; sentinel
       (#1568) sends no `agent_id` on this path at all, so excluding this
       form costs it nothing.
    4. A resolution that FAILED must not corroborate merely because it
       carries a matching ``agent_uuid``. `_substrate_http_reject`
       (`identity/resolution.py`) returns exactly that shape —
       ``resume_failed=True`` + the REJECTED uuid — for a substrate-anchored
       resident resumed over HTTP instead of its attested UDS socket; a
       third codex review caught that the equality check alone reads a
       refusal as a match.

    The ``continuity_token`` branch needs the matching discipline for the
    same reason: `extract_token_agent_uuid_safe` performs the HMAC
    signature verification single-sourced in `identity/session.py`, but a
    token only corroborates when it ALSO verifies AND the uuid it embeds
    matches the ``agent_id`` being claimed — otherwise a caller could
    corroborate agent A's uuid with a valid token for agent B. It has no
    canonical-form problem: a token is signed, never a deterministic
    function of the uuid alone.

    The session lookup added for check 2 is the first non-trivial I/O this
    function performs — bounded to `_CORROBORATION_LOOKUP_TIMEOUT` so an
    unresponsive Redis/Postgres degrades this call to "none" (fall through,
    same as an unresolvable claim) rather than stalling every REST call
    that names an explicit agent_id, matching this codebase's established
    `asyncio.wait_for`-guarded-degrade pattern for exactly this dependency
    (see CLAUDE.md "Substrate Tax", `identity_step.py`'s Redis guards).
    """
    if not isinstance(arguments, dict):
        return "none"
    claimed_agent_id = arguments.get("agent_id")
    client_session_id = arguments.get("client_session_id")
    if isinstance(client_session_id, str) and client_session_id:
        from src.mcp_handlers.context import get_csid_transport_injected
        from src.mcp_handlers.identity.session import normalize_client_session_id
        from src.mcp_handlers.identity.shared import make_client_session_id

        # Normalize BEFORE comparing for the canonical-form exclusion, not
        # after: comparing the raw value and resolving the normalized one
        # let a whitespace-padded canonical id (`"  agent-<uuid12>  "`)
        # dodge the exclusion check and still resolve to the forgeable
        # canonical session key — found live by a fourth codex review.
        normalized = normalize_client_session_id(client_session_id)

        is_canonical_prefix_form = False
        if normalized and isinstance(claimed_agent_id, str):
            try:
                is_canonical_prefix_form = normalized == make_client_session_id(
                    claimed_agent_id
                )
            except ValueError:
                pass

        if not get_csid_transport_injected() and not is_canonical_prefix_form:
            from src.mcp_handlers.identity.handlers import resolve_session_identity

            if normalized:
                try:
                    resolved = await asyncio.wait_for(
                        resolve_session_identity(normalized, persist=False, resume=True),
                        timeout=_CORROBORATION_LOOKUP_TIMEOUT,
                    )
                except Exception as exc:
                    logger.debug(
                        "[ATTEST] csid corroboration lookup failed/timed out: %s", exc
                    )
                    resolved = None
                if (
                    resolved
                    and not resolved.get("created")
                    and not resolved.get("resume_failed")
                    and not resolved.get("error")
                    and resolved.get("agent_uuid") == claimed_agent_id
                ):
                    return "csid"
    continuity_token = arguments.get("continuity_token")
    if isinstance(continuity_token, str) and continuity_token:
        from src.mcp_handlers.identity.session import extract_token_agent_uuid_safe

        token_agent_uuid = extract_token_agent_uuid_safe(continuity_token)
        if token_agent_uuid and token_agent_uuid == claimed_agent_id:
            return "token"
    return "none"


def _looks_like_uuid(value: object) -> bool:
    """Shape check only (36 chars, 4 hyphens) — no lookup, no verification.

    Single-sourced so a downstream consumer's "is this UUID-shaped, or a
    legacy handle" branch stays in lockstep with what actually reaches the
    explicit-bind path.
    """
    return (
        isinstance(value, str)
        and len(value) == 36
        and value.count("-") == 4
    )


async def _bind_explicit_http_agent(arguments: dict) -> str | None:
    explicit_agent_id = arguments.get("agent_id")
    if not _looks_like_uuid(explicit_agent_id):
        return None

    # GATE — armed 2026-08-24. This branch used to accept an identity on the
    # caller's word: the test above is a *shape* check (36 chars, 4 hyphens),
    # not a lookup, and it ran first in `_resolve_http_prebind`, ahead of the
    # operator token and the sticky binding. A resolution that succeeded here
    # meant the strict-identity gate downstream never saw a miss, so it never
    # emitted its typed refusal — the surface the trust-anchor audit is scoped
    # to. Corroboration turns the shape check into a proof check: a bare uuid
    # is a payload field (who this call is *about*), not a credential (who is
    # *asking*), and must not be treated as the latter.
    #
    # This ran as an observation-only canary (PR #1566) per the fleet rule
    # that a gate needs a wired canary before it is armed. Precondition:
    # sentinel was the one caller measured relying on the bare-uuid path
    # (#1565) and was fixed to bind by CSID instead (#1568).
    #
    # ⛔ The canary's own reading is NOT the evidence for this arming. The
    # instrument it ran with (this PR's first draft) counted a
    # transport-injected `client_session_id` as corroboration — and the
    # transport injects one into EVERY REST call missing it — so its
    # "298,573 calls, 100% csid, 0% none" measured the transport's own
    # filler, not caller behaviour. It also, in the draft one round later,
    # counted ANY real session as corroboration for ANY claimed agent_id —
    # not just the caller's own — so even a corrected re-run of that count
    # would still have overstated how many callers were actually proving
    # THIS claim. Both fixed same PR (see this function's docstring and
    # `_explicit_bind_corroboration`'s), but not re-measured against live
    # traffic before arming: closing a live impersonation bypass took
    # priority over waiting out a second canary window. The only population
    # this can newly refuse under the corrected classifier is a caller
    # offering NEITHER a caller-asserted client_session_id (body or header)
    # that resolves to THIS agent_id NOR a token that verifies and names
    # THIS agent_id — i.e. no proof of any kind that this claim is theirs,
    # which is the exact case #1565 already proved sentinel does not fall
    # into (sentinel binds by its own CSID naming its own uuid).
    corroboration = await _explicit_bind_corroboration(arguments)

    # Deliberately NOT logged: the uuid itself, at any length. A prefix is
    # still an identity fragment, and this line is meant to be safe to leave
    # on in a live server and safe to read in a shared log.
    logger.info(
        "[ATTEST] explicit_agent_id bind corroboration=%s tool_arg_keys=%d",
        corroboration,
        len(arguments) if isinstance(arguments, dict) else 0,
    )

    if corroboration == "none":
        # No lookup, no proof of ownership — fall through to the next
        # resolution step (operator token, sticky binding, session binding)
        # exactly as an unrecognized shape does. If none of those resolve
        # either, the strict-identity gate downstream emits its typed
        # identity_required refusal.
        return None

    from src.mcp_handlers.context import update_context_agent_id

    update_context_agent_id(explicit_agent_id)
    return explicit_agent_id


def _preserve_explicit_target(arguments: dict, resolved_uuid: str) -> None:
    """Stamp the resolved identity as the call target ONLY if none was asked for.

    Every prebind path resolves two different things at once: WHO is calling
    (the context binding, which authorizes) and WHAT the call is about (the
    ``agent_id`` argument, which selects). They coincide for the common
    self-read, so writing the resolved uuid into ``arguments`` was harmless
    there and wrong whenever a caller named a target — the read came back for
    the caller instead, under a `success` envelope, with the response's own
    ``agent_id`` field quietly reading the substituted identity.

    A caller that names no target still gets the resolved identity stamped, so
    the self-read path is unchanged.
    """
    if not arguments.get("agent_id"):
        arguments["agent_id"] = resolved_uuid


async def _resolve_http_operator(arguments: dict, signals) -> str | None:
    from src.mcp_handlers.context import (
        set_session_proof_origin,
        set_session_resolution_source,
        update_context_agent_id,
    )
    from src.mcp_handlers.identity.operator import resolve_operator_identity

    try:
        operator_identity = await resolve_operator_identity(signals)
    except Exception as exc:
        logger.warning("[OPERATOR] identity resolution failed: %s", exc)
        return None
    if not operator_identity:
        return None
    agent_uuid = operator_identity["agent_uuid"]
    update_context_agent_id(agent_uuid)
    # Bind the CALLER, do not retarget the CALL. Overwriting a caller-supplied
    # agent_id here answered a question about agent X with agent Y's state and
    # said nothing about it — the silent-substitution shape invariant 1 exists
    # to forbid. It only bit a NON-uuid-shaped agent_id, because
    # _bind_explicit_http_agent returns early on the uuid shape and never
    # reaches this branch; a structured handle
    # (`Claude_Code_<date>_<uuid8>`) fell straight through. Since #1533 a
    # read-state response reports `agent_id` AS that handle, so a caller
    # round-tripping the field it was just handed was exactly the caller that
    # got silently redirected to itself.
    _preserve_explicit_target(arguments, agent_uuid)
    set_session_resolution_source("operator_token")
    set_session_proof_origin("caller_asserted")
    return agent_uuid


async def _consult_http_sticky_binding(arguments: dict, signals):
    from src.mcp_handlers.context import (
        set_session_proof_origin,
        set_session_resolution_source,
        update_context_agent_id,
    )
    from src.mcp_handlers.middleware.identity_step import (
        consult_sticky_binding,
        sticky_resolution_source,
    )

    try:
        consult = await consult_sticky_binding(
            signals, arguments, redis_recovery=False
        )
        if consult.binding is None:
            return None, consult
        cached = consult.binding
        update_context_agent_id(cached.agent_uuid)
        # Same split as the operator path: a sticky binding says who the
        # transport belongs to, never what the caller asked about. This one is
        # strictly worse when it retargets — proof_origin is
        # "server_inferred", so it silently substituted an identity the caller
        # never asserted at all.
        _preserve_explicit_target(arguments, cached.agent_uuid)
        set_session_resolution_source(sticky_resolution_source(cached))
        set_session_proof_origin("server_inferred")
        return cached.agent_uuid, consult
    except Exception as exc:
        logger.debug("[STICKY-REST] cache check failed: %s", exc)
        return None, None


def _cache_http_resolution(
    consult,
    resolved: dict,
    agent_uuid: str,
    session_key: str,
) -> None:
    writeback_key = (
        consult.transport_key if (consult and consult.cacheable) else None
    )
    if not writeback_key:
        return
    try:
        from src.mcp_handlers.middleware.identity_step import update_transport_binding

        update_transport_binding(
            writeback_key,
            agent_uuid,
            session_key,
            source="rest",
            original_session_source=resolved.get("source") or "rest_resolution",
        )
    except Exception as exc:
        logger.debug("[STICKY-REST] cache update failed: %s", exc)


async def _touch_http_session_activity(
    session_key: str,
    agent_uuid: str,
) -> None:
    for attempt in range(2):
        try:
            from src.db import get_db

            await get_db().update_session_activity(session_key)
            return
        except Exception as exc:
            if attempt == 0:
                await asyncio.sleep(0.05)
            else:
                logger.warning(
                    "[REST-SESSION] TTL update failed for agent %s...: %s",
                    agent_uuid[:8],
                    exc,
                )


async def _resolve_http_session_binding(
    tool_name: str,
    arguments: dict,
    signals,
    consult,
) -> str | None:
    from src.mcp_handlers.context import update_context_agent_id
    from src.mcp_handlers.identity.handlers import (
        derive_session_key,
        resolve_session_identity,
    )
    from src.mcp_handlers.identity.session import extract_token_agent_uuid_safe

    session_key = await derive_session_key(signals, arguments)
    token_agent_uuid = extract_token_agent_uuid_safe(
        arguments.get("continuity_token")
    )
    if not token_agent_uuid:
        from src.mcp_handlers.decorators import get_call_identity_requirement

        if get_call_identity_requirement(tool_name, arguments) == "pre_onboard":
            return None

    resolved = await resolve_session_identity(
        session_key,
        persist=False,
        model_type=arguments.get("model_type"),
        client_hint=arguments.get("client_hint"),
        resume=True,
        token_agent_uuid=token_agent_uuid,
    )
    if not resolved or resolved.get("created"):
        return None
    agent_uuid = resolved.get("agent_uuid")
    if not agent_uuid:
        return None

    update_context_agent_id(agent_uuid)
    # Third and last prebind path, same rule (see _preserve_explicit_target):
    # a resumed session binding identifies the caller, not the target.
    _preserve_explicit_target(arguments, agent_uuid)
    _cache_http_resolution(consult, resolved, agent_uuid, session_key)
    await _touch_http_session_activity(session_key, agent_uuid)
    return agent_uuid


async def _resolve_http_bound_agent(
    tool_name: str,
    arguments: dict,
    signals,
) -> str | None:
    """Resolve an existing identity before dispatching a direct HTTP tool."""
    if not isinstance(arguments, dict) or tool_name in _HTTP_PREBIND_SKIP_TOOLS:
        return None

    explicit_agent_id = await _bind_explicit_http_agent(arguments)
    if explicit_agent_id:
        return explicit_agent_id

    operator_agent_id = await _resolve_http_operator(arguments, signals)
    if operator_agent_id:
        return operator_agent_id

    cached_agent_id, consult = await _consult_http_sticky_binding(
        arguments, signals
    )
    if cached_agent_id:
        return cached_agent_id

    return await _resolve_http_session_binding(
        tool_name, arguments, signals, consult
    )


def _http_bool(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes")
