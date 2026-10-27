"""Passkey authentication and opaque dashboard sessions.

The browser auth surface is deliberately separate from MCP authentication.
Hosted MCP bearer posture never consults these helpers; ``http_api`` only
accepts a validated dashboard session in its local/self-host branch.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from src.db import get_db
from src.logging_utils import get_logger

logger = get_logger(__name__)

def _dashboard_webauthn_config() -> tuple[str, str]:
    """Read the passkey relying party; unset means passkey sign-in is off.

    A passkey is bound to one domain and a browser refuses it anywhere else,
    so no default RP id works for every deployment: the old fallback to one
    operator's domain left every other install with passkey setup that could
    never succeed. The origin defaults to https:// plus the RP id.
    """
    rp_id = os.getenv("UNITARES_DASHBOARD_RP_ID", "").strip()
    if not rp_id:
        return "", ""
    expected_origin = (
        os.getenv("UNITARES_DASHBOARD_ORIGIN", "").strip() or f"https://{rp_id}"
    )
    return rp_id, expected_origin


DASHBOARD_RP_ID, DASHBOARD_EXPECTED_ORIGIN = _dashboard_webauthn_config()
DASHBOARD_RP_NAME = "UNITARES Governance"


def _passkeys_unconfigured() -> JSONResponse | None:
    """Refuse every passkey ceremony, naming the fix, when no RP id is set."""
    if DASHBOARD_RP_ID:
        return None
    return JSONResponse(
        {
            "error": "passkey sign-in is not configured",
            "fix": (
                "set UNITARES_DASHBOARD_RP_ID to the domain the dashboard is "
                "served from, and UNITARES_DASHBOARD_ORIGIN if its origin is "
                "not https:// plus that domain"
            ),
        },
        status_code=503,
    )

SESSION_COOKIE = "__Host-unitares_session"
PREAUTH_COOKIE = "__Host-unitares_preauth"
SESSION_SLIDING_SECONDS = 30 * 24 * 60 * 60
SESSION_HARD_SECONDS = 90 * 24 * 60 * 60
CHALLENGE_TTL_SECONDS = 120
ENROLL_CODE_TTL_SECONDS = 10 * 60
STEP_UP_TTL_SECONDS = 10 * 60
CSRF_HEADER = "x-unitares-csrf"
ENROLL_CODE_HEADER = "x-unitares-enroll-code"

_OPTIONS_RATE_LIMIT = 20
_OPTIONS_RATE_WINDOW_SECONDS = 60
_options_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _sha256(value: str | bytes) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).digest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _operator_label() -> str:
    return os.getenv("UNITARES_DASHBOARD_OPERATOR_LABEL", "operator").strip() or "operator"


def _session_state(connection) -> dict[str, Any] | None:
    state = getattr(connection, "state", None)
    session = getattr(state, "dashboard_session", None) if state is not None else None
    return session if isinstance(session, dict) else None


def dashboard_session_authenticated(connection) -> bool:
    """Return whether middleware attached a live DB-backed dashboard session."""
    return _session_state(connection) is not None


def dashboard_session_write_authorized(connection) -> bool:
    """Cookie write authorization: live session plus the non-simple CSRF header."""
    return dashboard_session_authenticated(connection) and secrets.compare_digest(
        connection.headers.get(CSRF_HEADER, ""), "1"
    )


def dashboard_session_is_fresh(connection) -> bool:
    session = _session_state(connection)
    if not session:
        return False
    created_at = session.get("created_at")
    if not isinstance(created_at, datetime):
        return False
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return now - created_at <= timedelta(seconds=STEP_UP_TTL_SECONDS)


async def load_dashboard_session(connection) -> dict[str, Any] | None:
    """Validate the opaque cookie against Postgres and slide its soft expiry.

    Missing tables (before manual migration 057), DB outages, expired sessions,
    and revoked credentials all fail closed. The raw cookie never reaches logs
    or storage; only its SHA-256 digest is queried.
    """
    raw_session = connection.cookies.get(SESSION_COOKIE)
    if not raw_session:
        return None
    session_hash = _sha256(raw_session)
    try:
        db = get_db()
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.session_hash, s.credential_id, s.operator_label,
                       s.created_at, s.expires_at, s.hard_expires_at,
                       s.last_seen_at, s.user_agent
                  FROM core.dashboard_sessions s
                  JOIN core.webauthn_credentials c
                    ON c.credential_id = s.credential_id
                 WHERE s.session_hash = $1
                   AND s.revoked_at IS NULL
                   AND c.revoked_at IS NULL
                   AND s.expires_at > now()
                   AND s.hard_expires_at > now()
                """,
                session_hash,
            )
            if not row:
                return None
            last_seen_at = row["last_seen_at"]
            if last_seen_at is None or (
                datetime.now(timezone.utc) - last_seen_at
                >= timedelta(minutes=5)
            ):
                await conn.execute(
                    """
                    UPDATE core.dashboard_sessions
                       SET last_seen_at = now(),
                           expires_at = LEAST(hard_expires_at,
                               now() + interval '30 days')
                     WHERE session_hash = $1
                    """,
                    session_hash,
                )
        session = dict(row)
        session["session_hash"] = session_hash
        return session
    except Exception as exc:  # auth storage unavailable => deny, never fail open
        logger.debug("dashboard session validation failed closed: %s", exc)
        return None


async def attach_dashboard_session(connection) -> None:
    """Populate shared ASGI scope state for sync REST/WS auth gates."""
    session = await load_dashboard_session(connection)
    if session is not None:
        connection.state.dashboard_session = session


def _operator_token_authorized(request) -> bool:
    from src.mcp_handlers.context import SessionSignals
    from src.mcp_handlers.identity.operator import is_operator_caller

    return is_operator_caller(
        SessionSignals(
            transport="rest",
            unitares_operator_token=request.headers.get("x-unitares-operator"),
        )
    )


def _normalize_enroll_code(value: str | None) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def _enroll_code_from_request(request) -> str:
    # D3: enrollment secrets must never enter URLs (history sync, access logs,
    # referrers, and screenshots).  The dashboard collects a typed code and
    # presents it only in this request header.
    return _normalize_enroll_code(request.headers.get(ENROLL_CODE_HEADER))


async def _enroll_code_valid(code: str) -> bool:
    if not code:
        return False
    try:
        db = get_db()
        async with db.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM core.webauthn_enroll_codes
                         WHERE code_hash = $1
                           AND used_at IS NULL
                           AND expires_at > now()
                    )
                    """,
                    _sha256(code),
                )
            )
    except Exception as exc:
        logger.debug("enrollment code validation failed closed: %s", exc)
        return False


async def _registration_authorized(request) -> tuple[bool, str | None]:
    if _operator_token_authorized(request):
        return True, None
    code = _enroll_code_from_request(request)
    return (await _enroll_code_valid(code), code or None)


def _rate_limit_authentication_options(request) -> bool:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _options_rate_buckets[key]
    cutoff = now - _OPTIONS_RATE_WINDOW_SECONDS
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if len(bucket) >= _OPTIONS_RATE_LIMIT:
        return False
    bucket.append(now)
    return True


def _preauth_value(request) -> str:
    return request.cookies.get(PREAUTH_COOKIE) or secrets.token_urlsafe(32)


async def _store_challenge(preauth: str, challenge: bytes, ceremony: str) -> None:
    db = get_db()
    async with db.acquire() as conn:
        async with conn.transaction():
            # The unauthenticated options route is floodable. Sweep on every
            # challenge write so garbage collection is never success-gated.
            await conn.execute(
                "DELETE FROM core.webauthn_challenges WHERE expires_at <= now()"
            )
            await conn.execute(
                """
                INSERT INTO core.webauthn_challenges
                    (pre_session_hash, challenge, ceremony, created_at, expires_at)
                VALUES ($1, $2, $3, now(), now() + interval '120 seconds')
                ON CONFLICT (pre_session_hash) DO UPDATE
                    SET challenge = EXCLUDED.challenge,
                        ceremony = EXCLUDED.ceremony,
                        created_at = EXCLUDED.created_at,
                        expires_at = EXCLUDED.expires_at
                """,
                _sha256(preauth),
                challenge,
                ceremony,
            )


async def _consume_challenge(request, expected_ceremony: str) -> bytes | None:
    preauth = request.cookies.get(PREAUTH_COOKIE)
    if not preauth:
        return None
    try:
        db = get_db()
        async with db.acquire() as conn:
            # Delete before cryptographic verification: every challenge is
            # single-use even when the response is invalid or mismatched.
            row = await conn.fetchrow(
                """
                DELETE FROM core.webauthn_challenges
                 WHERE pre_session_hash = $1
                 RETURNING challenge, ceremony, expires_at
                """,
                _sha256(preauth),
            )
        if not row:
            return None
        expires_at = row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if row["ceremony"] != expected_ceremony or expires_at <= datetime.now(timezone.utc):
            return None
        return bytes(row["challenge"])
    except Exception as exc:
        logger.warning("challenge consume failed closed: %s", exc)
        return None


def _set_preauth_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        PREAUTH_COOKIE,
        value,
        max_age=CHALLENGE_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _set_session_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=SESSION_SLIDING_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_cookie(response: Response, name: str) -> None:
    response.set_cookie(
        name,
        "",
        max_age=0,
        expires=0,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


async def _active_credential_count() -> int:
    try:
        db = get_db()
        async with db.acquire() as conn:
            return int(
                await conn.fetchval(
                    "SELECT count(*) FROM core.webauthn_credentials WHERE revoked_at IS NULL"
                )
                or 0
            )
    except Exception:
        return 0


async def _emit_auth_event(event_type: str, operator_label: str, **payload: Any) -> None:
    """Broadcast and persist browser-auth audit events without secret material."""
    try:
        from src.broadcaster import broadcaster_instance

        await broadcaster_instance.broadcast_event(
            event_type,
            payload={"operator_label": operator_label, **payload},
        )
    except Exception as exc:
        logger.warning("auth audit event %s failed: %s", event_type, exc)


def _webauthn_imports() -> dict[str, Any]:
    """Lazy import keeps the core (non-``full``) install importable."""
    from webauthn import (
        generate_authentication_options,
        generate_registration_options,
        options_to_json,
        verify_authentication_response,
        verify_registration_response,
    )
    from webauthn.helpers import (
        parse_authentication_credential_json,
        parse_registration_credential_json,
    )
    from webauthn.helpers.structs import (
        AttestationConveyancePreference,
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    return {
        "generate_authentication_options": generate_authentication_options,
        "generate_registration_options": generate_registration_options,
        "options_to_json": options_to_json,
        "verify_authentication_response": verify_authentication_response,
        "verify_registration_response": verify_registration_response,
        "parse_authentication_credential_json": parse_authentication_credential_json,
        "parse_registration_credential_json": parse_registration_credential_json,
        "AttestationConveyancePreference": AttestationConveyancePreference,
        "AuthenticatorSelectionCriteria": AuthenticatorSelectionCriteria,
        "PublicKeyCredentialDescriptor": PublicKeyCredentialDescriptor,
        "ResidentKeyRequirement": ResidentKeyRequirement,
        "UserVerificationRequirement": UserVerificationRequirement,
    }


def _auth_page(name: str) -> Response:
    base = Path(__file__).parent.parent / "dashboard" / "redesign" / "auth"
    target = base / name
    if not target.is_file():
        return HTMLResponse("Dashboard authentication UI is not installed.", status_code=503)
    content = target.read_text().replace(
        "{{DASHBOARD_RP_ID}}", html.escape(DASHBOARD_RP_ID)
    )
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


async def http_auth_signin(request):
    """GET /auth/signin — public, but inert until a credential is enrolled."""
    if unconfigured := _passkeys_unconfigured():
        return unconfigured
    enrollment_start = request.query_params.get("enroll") == "1"
    step_up = request.query_params.get("stepup") == "1"
    if dashboard_session_authenticated(request) and not step_up and not enrollment_start:
        return RedirectResponse("/", status_code=303)
    if await _active_credential_count() == 0 and not enrollment_start:
        return RedirectResponse("/", status_code=303)
    return _auth_page("signin.html")


async def http_auth_enroll(request):
    """GET enrollment UI; POST mints a 10-minute single-use bootstrap code."""
    if unconfigured := _passkeys_unconfigured():
        return unconfigured
    if request.method == "POST":
        if not _operator_token_authorized(request):
            return JSONResponse({"error": "operator credential required"}, status_code=403)
        alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
        normalized = "".join(secrets.choice(alphabet) for _ in range(10))
        display = f"{normalized[:5]}-{normalized[5:]}"
        try:
            db = get_db()
            async with db.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM core.webauthn_enroll_codes WHERE expires_at <= now()"
                    )
                    await conn.execute(
                        """
                        INSERT INTO core.webauthn_enroll_codes
                            (code_hash, created_at, expires_at)
                        VALUES ($1, now(), now() + interval '10 minutes')
                        """,
                        _sha256(normalized),
                    )
            return JSONResponse({"code": display, "expires_in": ENROLL_CODE_TTL_SECONDS})
        except Exception as exc:
            logger.warning("enrollment code mint failed: %s", exc)
            return JSONResponse({"error": "authentication storage unavailable"}, status_code=503)

    allowed, _ = await _registration_authorized(request)
    if not allowed:
        return JSONResponse({"error": "valid enrollment code required"}, status_code=403)
    return _auth_page("enroll.html")


async def http_webauthn_options(request):
    """Create usernameless assertion options without credential enumeration."""
    if unconfigured := _passkeys_unconfigured():
        return unconfigured
    if not _rate_limit_authentication_options(request):
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
    if await _active_credential_count() == 0:
        return JSONResponse({"error": "no passkeys enrolled"}, status_code=409)
    try:
        wa = _webauthn_imports()
        challenge = secrets.token_bytes(32)
        preauth = _preauth_value(request)
        options = wa["generate_authentication_options"](
            rp_id=DASHBOARD_RP_ID,
            challenge=challenge,
            timeout=CHALLENGE_TTL_SECONDS * 1000,
            allow_credentials=[],
            user_verification=wa["UserVerificationRequirement"].REQUIRED,
        )
        await _store_challenge(preauth, challenge, "authenticate")
        response = JSONResponse(json.loads(wa["options_to_json"](options)))
        _set_preauth_cookie(response, preauth)
        return response
    except Exception as exc:
        logger.warning("authentication options failed: %s", exc)
        return JSONResponse({"error": "authentication unavailable"}, status_code=503)


async def http_webauthn_verify(request):
    """Consume an assertion challenge, verify it, and create an opaque session."""
    if unconfigured := _passkeys_unconfigured():
        return unconfigured
    challenge = await _consume_challenge(request, "authenticate")
    if challenge is None:
        return JSONResponse({"error": "challenge missing, expired, or already used"}, status_code=400)
    try:
        body = await request.json()
        credential_body = body.get("credential", body) if isinstance(body, dict) else body
        wa = _webauthn_imports()
        parsed = wa["parse_authentication_credential_json"](credential_body)
        db = get_db()
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT credential_id, public_key, user_handle, sign_count, operator_label
                  FROM core.webauthn_credentials
                 WHERE credential_id = $1 AND revoked_at IS NULL
                """,
                bytes(parsed.raw_id),
            )
        if not row:
            return JSONResponse({"error": "credential not recognized"}, status_code=401)
        response_user_handle = parsed.response.user_handle
        if response_user_handle is None or not secrets.compare_digest(
            bytes(response_user_handle), bytes(row["user_handle"])
        ):
            return JSONResponse({"error": "credential user handle mismatch"}, status_code=401)
        verified = wa["verify_authentication_response"](
            credential=parsed,
            expected_challenge=challenge,
            expected_rp_id=DASHBOARD_RP_ID,
            expected_origin=DASHBOARD_EXPECTED_ORIGIN,
            credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=int(row["sign_count"]),
            require_user_verification=True,
        )
        raw_session = secrets.token_urlsafe(32)
        session_hash = _sha256(raw_session)
        user_agent = request.headers.get("user-agent", "")[:1000]
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE core.webauthn_credentials
                       SET sign_count = CASE WHEN $2 > 0 THEN $2 ELSE sign_count END,
                           last_used_at = now()
                     WHERE credential_id = $1 AND revoked_at IS NULL
                    """,
                    bytes(row["credential_id"]),
                    int(verified.new_sign_count),
                )
                await conn.execute(
                    """
                    INSERT INTO core.dashboard_sessions
                        (session_hash, credential_id, operator_label, created_at,
                         expires_at, hard_expires_at, last_seen_at, user_agent)
                    VALUES ($1, $2, $3, now(), now() + interval '30 days',
                            now() + interval '90 days', now(), $4)
                    """,
                    session_hash,
                    bytes(row["credential_id"]),
                    row["operator_label"],
                    user_agent,
                )
        response = JSONResponse({"success": True, "redirect": "/"})
        _set_session_cookie(response, raw_session)
        _clear_cookie(response, PREAUTH_COOKIE)
        await _emit_auth_event(
            "dashboard_signin",
            row["operator_label"],
            credential_id=_b64url(bytes(row["credential_id"])),
        )
        return response
    except Exception as exc:
        logger.info("passkey assertion rejected: %s", exc)
        return JSONResponse({"error": "passkey verification failed"}, status_code=401)


async def http_webauthn_register_options(request):
    if unconfigured := _passkeys_unconfigured():
        return unconfigured
    allowed, _ = await _registration_authorized(request)
    if not allowed:
        return JSONResponse({"error": "fresh operator credential or enrollment code required"}, status_code=403)
    try:
        wa = _webauthn_imports()
        db = get_db()
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT credential_id FROM core.webauthn_credentials WHERE revoked_at IS NULL"
            )
        challenge = secrets.token_bytes(32)
        preauth = _preauth_value(request)
        user_handle = _sha256(f"unitares-dashboard:{DASHBOARD_RP_ID}:{_operator_label()}")
        descriptors = [
            wa["PublicKeyCredentialDescriptor"](id=bytes(row["credential_id"]))
            for row in rows
        ]
        options = wa["generate_registration_options"](
            rp_id=DASHBOARD_RP_ID,
            rp_name=DASHBOARD_RP_NAME,
            user_name=_operator_label(),
            user_display_name=_operator_label(),
            user_id=user_handle,
            challenge=challenge,
            timeout=CHALLENGE_TTL_SECONDS * 1000,
            attestation=wa["AttestationConveyancePreference"].NONE,
            authenticator_selection=wa["AuthenticatorSelectionCriteria"](
                resident_key=wa["ResidentKeyRequirement"].REQUIRED,
                require_resident_key=True,
                user_verification=wa["UserVerificationRequirement"].REQUIRED,
            ),
            exclude_credentials=descriptors,
        )
        await _store_challenge(preauth, challenge, "register")
        response = JSONResponse(json.loads(wa["options_to_json"](options)))
        _set_preauth_cookie(response, preauth)
        return response
    except Exception as exc:
        logger.warning("registration options failed: %s", exc)
        return JSONResponse({"error": "registration unavailable"}, status_code=503)


async def http_webauthn_register_verify(request):
    if unconfigured := _passkeys_unconfigured():
        return unconfigured
    allowed, enroll_code = await _registration_authorized(request)
    if not allowed:
        return JSONResponse({"error": "fresh operator credential or enrollment code required"}, status_code=403)
    challenge = await _consume_challenge(request, "register")
    if challenge is None:
        return JSONResponse({"error": "challenge missing, expired, or already used"}, status_code=400)
    try:
        body = await request.json()
        credential_body = body.get("credential", body) if isinstance(body, dict) else body
        nickname = str(body.get("nickname") or "").strip()[:120] or None
        wa = _webauthn_imports()
        parsed = wa["parse_registration_credential_json"](credential_body)
        verified = wa["verify_registration_response"](
            credential=parsed,
            expected_challenge=challenge,
            expected_rp_id=DASHBOARD_RP_ID,
            expected_origin=DASHBOARD_EXPECTED_ORIGIN,
            require_user_verification=True,
        )
        transports = [
            getattr(value, "value", str(value)) for value in (parsed.response.transports or [])
        ]
        operator_label = _operator_label()
        user_handle = _sha256(f"unitares-dashboard:{DASHBOARD_RP_ID}:{operator_label}")
        db = get_db()
        async with db.acquire() as conn:
            async with conn.transaction():
                if enroll_code:
                    consumed = await conn.fetchval(
                        """
                        UPDATE core.webauthn_enroll_codes
                           SET used_at = now()
                         WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now()
                        RETURNING true
                        """,
                        _sha256(enroll_code),
                    )
                    if not consumed:
                        return JSONResponse({"error": "enrollment code already used or expired"}, status_code=403)
                await conn.execute(
                    """
                    INSERT INTO core.webauthn_credentials
                        (credential_id, public_key, user_handle, sign_count,
                         transports, operator_label, nickname)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    bytes(verified.credential_id),
                    bytes(verified.credential_public_key),
                    user_handle,
                    int(verified.sign_count),
                    transports,
                    operator_label,
                    nickname,
                )
        credential_id = _b64url(bytes(verified.credential_id))
        await _emit_auth_event(
            "webauthn_enrolled",
            operator_label,
            credential_id=credential_id,
            nickname=nickname,
            notification_required=True,
            severity="critical",
            message="A new UNITARES dashboard passkey was enrolled",
        )
        response = JSONResponse({"success": True, "credential_id": credential_id})
        _clear_cookie(response, PREAUTH_COOKIE)
        return response
    except Exception as exc:
        logger.info("passkey registration rejected: %s", exc)
        return JSONResponse({"error": "passkey registration failed"}, status_code=400)


def _session_gate(request) -> JSONResponse | None:
    if not dashboard_session_write_authorized(request):
        return JSONResponse(
            {"error": "dashboard session and X-Unitares-Csrf: 1 required"},
            status_code=403,
        )
    return None


async def http_auth_logout(request):
    denied = _session_gate(request)
    if denied:
        return denied
    session = _session_state(request)
    try:
        db = get_db()
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE core.dashboard_sessions SET revoked_at = now() WHERE session_hash = $1",
                session["session_hash"],
            )
        await _emit_auth_event("dashboard_session_revoked", session["operator_label"], scope="current")
        response = JSONResponse({"success": True})
        _clear_cookie(response, SESSION_COOKIE)
        return response
    except Exception as exc:
        logger.warning("dashboard logout failed: %s", exc)
        return JSONResponse({"error": "authentication storage unavailable"}, status_code=503)


async def http_auth_sessions(request):
    denied = _session_gate(request)
    if denied:
        return denied
    session = _session_state(request)
    try:
        db = get_db()
        async with db.acquire() as conn:
            if request.method == "POST":
                result = await conn.execute(
                    """
                    UPDATE core.dashboard_sessions SET revoked_at = now()
                     WHERE operator_label = $1 AND revoked_at IS NULL
                    """,
                    session["operator_label"],
                )
                await _emit_auth_event(
                    "dashboard_session_revoked",
                    session["operator_label"],
                    scope="all",
                    count=int(str(result).split()[-1]),
                )
                response = JSONResponse({"success": True})
                _clear_cookie(response, SESSION_COOKIE)
                return response
            sessions = await conn.fetch(
                """
                SELECT session_hash, credential_id, created_at, expires_at,
                       hard_expires_at, last_seen_at, user_agent
                  FROM core.dashboard_sessions
                 WHERE operator_label = $1 AND revoked_at IS NULL
                   AND expires_at > now() AND hard_expires_at > now()
                 ORDER BY last_seen_at DESC NULLS LAST, created_at DESC
                """,
                session["operator_label"],
            )
            credentials = await conn.fetch(
                """
                SELECT credential_id, nickname, transports, created_at, last_used_at
                  FROM core.webauthn_credentials
                 WHERE operator_label = $1 AND revoked_at IS NULL
                 ORDER BY created_at
                """,
                session["operator_label"],
            )
        return JSONResponse(
            {
                "sessions": [
                    {
                        "id": _b64url(bytes(row["session_hash"])),
                        "credential_id": _b64url(bytes(row["credential_id"])),
                        "created_at": row["created_at"].isoformat(),
                        "expires_at": row["expires_at"].isoformat(),
                        "hard_expires_at": row["hard_expires_at"].isoformat(),
                        "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
                        "user_agent": row["user_agent"],
                        "current": secrets.compare_digest(
                            bytes(row["session_hash"]), session["session_hash"]
                        ),
                    }
                    for row in sessions
                ],
                "credentials": [
                    {
                        "id": _b64url(bytes(row["credential_id"])),
                        "nickname": row["nickname"],
                        "transports": row["transports"] or [],
                        "created_at": row["created_at"].isoformat(),
                        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
                    }
                    for row in credentials
                ],
            }
        )
    except Exception as exc:
        logger.warning("dashboard session listing failed: %s", exc)
        return JSONResponse({"error": "authentication storage unavailable"}, status_code=503)


async def http_auth_credential_revoke(request):
    denied = _session_gate(request)
    if denied:
        return denied
    session = _session_state(request)
    try:
        credential_id = _b64url_decode(request.path_params.get("credential_id", ""))
    except Exception:
        return JSONResponse({"error": "invalid credential id"}, status_code=400)
    try:
        db = get_db()
        async with db.acquire() as conn:
            async with conn.transaction():
                target = await conn.fetchrow(
                    """
                    SELECT credential_id, operator_label
                      FROM core.webauthn_credentials
                     WHERE credential_id = $1 AND revoked_at IS NULL
                     FOR UPDATE
                    """,
                    credential_id,
                )
                if not target or target["operator_label"] != session["operator_label"]:
                    return JSONResponse({"error": "credential not found"}, status_code=404)
                active_count = int(
                    await conn.fetchval(
                        """
                        SELECT count(*) FROM core.webauthn_credentials
                         WHERE operator_label = $1 AND revoked_at IS NULL
                        """,
                        session["operator_label"],
                    )
                    or 0
                )
                if active_count <= 1 and not (
                    _operator_token_authorized(request) or dashboard_session_is_fresh(request)
                ):
                    return JSONResponse(
                        {"error": "last credential requires fresh passkey re-auth or operator token"},
                        status_code=403,
                    )
                await conn.execute(
                    "UPDATE core.webauthn_credentials SET revoked_at = now() WHERE credential_id = $1",
                    credential_id,
                )
                await conn.execute(
                    """
                    UPDATE core.dashboard_sessions SET revoked_at = now()
                     WHERE credential_id = $1 AND revoked_at IS NULL
                    """,
                    credential_id,
                )
        await _emit_auth_event(
            "webauthn_credential_revoked",
            session["operator_label"],
            credential_id=_b64url(credential_id),
        )
        response = JSONResponse({"success": True})
        if secrets.compare_digest(credential_id, session["credential_id"]):
            _clear_cookie(response, SESSION_COOKIE)
        return response
    except Exception as exc:
        logger.warning("credential revocation failed: %s", exc)
        return JSONResponse({"error": "authentication storage unavailable"}, status_code=503)


# ---------------------------------------------------------------------------
# Hub sign-in handoff (portal-hub-v0 H2b).
#
# The operator hub runs on loopback, where neither this deployment's passkeys
# (bound to UNITARES_DASHBOARD_RP_ID) nor its __Host- session cookie can reach
# it. So the hub borrows a sign-in instead:
#
#   1. The hub sends the browser to GET /auth/hub/authorize with a PKCE S256
#      challenge and its own state. A freshly signed-in dashboard session
#      sees a confirming page; anything older goes through the passkey first.
#   2. The page's POST (session, CSRF header, same Origin) mints a one-time
#      code, valid for HUB_CODE_TTL_SECONDS, and sends the browser to the one
#      configured callback with the code and the hub's state.
#   3. The hub exchanges code + verifier at POST /v1/hub/exchange for a hub
#      session handle, then asks POST /v1/hub/check on every connect and
#      write, and POST /v1/hub/revoke on sign-out.
#      A check is fresh while the passkey sign-in behind the parent session
#      is under STEP_UP_TTL_SECONDS old.
#
# The /v1/hub routes answer only over the owner-only UDS listener, never
# over TCP: the public tunnel arrives at a TCP listener from loopback, so the
# accepting socket is the only property of a request it cannot choose. They
# also need UNITARES_HUB_TOKEN, a credential scoped to these three routes.
# A hub session never extends its parent dashboard session; signing that
# session out, or revoking its passkey, ends every hub session made from it.
# Unconfigured (no valid UNITARES_HUB_CALLBACK_URL and UNITARES_HUB_TOKEN),
# every hub route answers 404.
# ---------------------------------------------------------------------------

HUB_CALLBACK_ENV = "UNITARES_HUB_CALLBACK_URL"
HUB_TOKEN_ENV = "UNITARES_HUB_TOKEN"
HUB_CODE_TTL_SECONDS = 60
HUB_SESSION_SECONDS = 24 * 60 * 60
HUB_TOKEN_MIN_LENGTH = 32
HUB_LOOPBACK_HOSTS = ("127.0.0.1", "::1")

_HUB_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_HUB_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_HUB_STATE_RE = re.compile(r"[A-Za-z0-9._~-]{16,128}")
_HUB_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def hub_config() -> tuple[dict[str, str] | None, str | None]:
    """Read the hub sign-in handoff settings; unset or invalid means every hub route answers 404.

    Returns ``(config, problem)``. ``config`` is ``None`` when the handoff is off, and ``problem`` then
    names why, without echoing a secret. The callback must be a plain
    ``http`` URL on a numeric loopback address with an explicit port and
    path, so a code can only ever be handed to a process on this machine.
    """
    callback = os.getenv(HUB_CALLBACK_ENV, "").strip()
    token = os.getenv(HUB_TOKEN_ENV, "").strip()
    if not callback:
        return None, f"{HUB_CALLBACK_ENV} is not set"
    try:
        parts = urlsplit(callback)
        port = parts.port
    except ValueError:
        return None, f"{HUB_CALLBACK_ENV} is not a valid URL"
    if (
        parts.scheme != "http"
        or parts.hostname not in HUB_LOOPBACK_HOSTS
        or port is None
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith("/")
        or parts.query
        or parts.fragment
    ):
        return None, (
            f"{HUB_CALLBACK_ENV} must be http://127.0.0.1:<port>/<path> "
            "(or [::1]) with no query, fragment or user info"
        )
    if len(token) < HUB_TOKEN_MIN_LENGTH:
        return None, f"{HUB_TOKEN_ENV} is not set or shorter than {HUB_TOKEN_MIN_LENGTH} characters"
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return {
        "callback": callback,
        "origin": f"http://{host}:{port}",
        "token": token,
    }, None


def _hub_unconfigured(config: dict[str, str] | None, problem: str | None) -> JSONResponse | None:
    if config is not None:
        return None
    return JSONResponse(
        {"error": "hub sign-in is not configured", "detail": problem},
        status_code=404,
    )


def _hub_authorize_params(source) -> tuple[dict[str, str] | None, str | None]:
    challenge = str(source.get("code_challenge") or "")
    method = str(source.get("code_challenge_method") or "")
    state = str(source.get("state") or "")
    if method != "S256":
        return None, "code_challenge_method must be S256"
    if not _HUB_CHALLENGE_RE.fullmatch(challenge):
        return None, "code_challenge must be a base64url SHA-256 digest (43 characters)"
    if not _HUB_STATE_RE.fullmatch(state):
        return None, "state must be 16 to 128 unreserved characters"
    return {"code_challenge": challenge, "state": state}, None


def _hub_signin_gate(request) -> Response | None:
    """Send a missing or stale dashboard session through the passkey first.

    Approving the hub creates a new credential holder, so it takes a session
    signed in within STEP_UP_TTL_SECONDS, as revoking the last passkey does.
    The sign-in page returns here afterwards (its ``next`` is restricted to
    this path).
    """
    if dashboard_session_is_fresh(request):
        return None
    target = request.url.path + ("?" + request.url.query if request.url.query else "")
    return RedirectResponse(
        "/auth/signin?stepup=1&next=" + quote(target, safe=""),
        status_code=303,
    )


_HUB_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    # The Allow button grants a sign-in, so no other page may frame it.
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


async def http_hub_authorize(request):
    """GET: the confirming page. POST: mint a one-time code for the hub."""
    config, problem = hub_config()
    if unconfigured := _hub_unconfigured(config, problem):
        return unconfigured
    if unconfigured := _passkeys_unconfigured():
        return unconfigured

    if request.method == "GET":
        _, invalid = _hub_authorize_params(request.query_params)
        if invalid:
            return JSONResponse({"error": invalid}, status_code=400)
        if redirect := _hub_signin_gate(request):
            return redirect
        base = Path(__file__).parent.parent / "dashboard" / "redesign" / "auth"
        target = base / "hub-authorize.html"
        if not target.is_file():
            return HTMLResponse("Dashboard authentication UI is not installed.", status_code=503)
        content = target.read_text().replace("{{HUB_ORIGIN}}", html.escape(config["origin"]))
        return HTMLResponse(content, headers=_HUB_PAGE_HEADERS)

    denied = _session_gate(request)
    if denied:
        return denied
    if request.headers.get("origin", "") != DASHBOARD_EXPECTED_ORIGIN:
        return JSONResponse({"error": "cross-origin hub approval refused"}, status_code=403)
    if not dashboard_session_is_fresh(request):
        return JSONResponse(
            {"error": "a passkey sign-in within the last ten minutes is required"},
            status_code=403,
        )
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    params, invalid = _hub_authorize_params(body)
    if invalid:
        return JSONResponse({"error": invalid}, status_code=400)

    session = _session_state(request)
    code = secrets.token_urlsafe(32)
    try:
        db = get_db()
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM core.hub_auth_codes WHERE expires_at <= now()")
                await conn.execute(
                    """
                    INSERT INTO core.hub_auth_codes
                        (code_hash, code_challenge, parent_session_hash,
                         credential_id, operator_label, created_at, expires_at)
                    VALUES ($1, $2, $3, $4, $5, now(),
                            now() + make_interval(secs => $6))
                    """,
                    _sha256(code),
                    params["code_challenge"],
                    session["session_hash"],
                    session["credential_id"],
                    session["operator_label"],
                    HUB_CODE_TTL_SECONDS,
                )
    except Exception as exc:
        logger.warning("hub code mint failed: %s", exc)
        return JSONResponse({"error": "authentication storage unavailable"}, status_code=503)
    await _emit_auth_event(
        "hub_signin_approved",
        session["operator_label"],
        credential_id=_b64url(bytes(session["credential_id"])),
        hub_origin=config["origin"],
    )
    redirect = (
        f"{config['callback']}?code={quote(code, safe='')}"
        f"&state={quote(params['state'], safe='')}"
    )
    return JSONResponse({"redirect": redirect}, headers={"Cache-Control": "no-store"})


def _hub_internal_gate(request, config: dict[str, str]) -> JSONResponse | None:
    """The /v1/hub routes: owner-only socket plus the hub credential."""
    scope = getattr(request, "scope", None) or {}
    peer_pid = scope.get("unitares_peer_pid")
    over_uds = isinstance(peer_pid, int) and not isinstance(peer_pid, bool) and peer_pid > 0
    if not over_uds or scope.get("unitares_public_listener"):
        return JSONResponse(
            {"error": "hub routes answer only on the server's local socket (UNITARES_UDS_SOCKET)"},
            status_code=403,
        )
    authorization = request.headers.get("authorization", "")
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        _sha256(presented.strip()), _sha256(config["token"])
    ):
        return JSONResponse({"error": "hub credential required"}, status_code=401)
    return None


async def _hub_internal_request(request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    config, problem = hub_config()
    if unconfigured := _hub_unconfigured(config, problem):
        return None, unconfigured
    if denied := _hub_internal_gate(request, config):
        return None, denied
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "JSON object body required"}, status_code=400)
    return body, None


def _hub_handle(body: dict[str, Any]) -> str | None:
    handle = body.get("handle")
    return handle if isinstance(handle, str) and _HUB_SECRET_RE.fullmatch(handle) else None


async def http_hub_exchange(request):
    """Exchange a one-time code and its PKCE verifier for a hub session."""
    body, denied = await _hub_internal_request(request)
    if denied:
        return denied
    code = body.get("code")
    verifier = body.get("code_verifier")
    if not (isinstance(code, str) and _HUB_SECRET_RE.fullmatch(code)):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if not (isinstance(verifier, str) and _HUB_VERIFIER_RE.fullmatch(verifier)):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    handle = secrets.token_urlsafe(32)
    try:
        db = get_db()
        async with db.acquire() as conn:
            async with conn.transaction():
                # Burn the code before checking the verifier: a code is
                # single-use even when the exchange is refused.
                row = await conn.fetchrow(
                    """
                    UPDATE core.hub_auth_codes
                       SET used_at = now()
                     WHERE code_hash = $1 AND used_at IS NULL
                     RETURNING code_challenge, parent_session_hash, credential_id,
                               operator_label, expires_at > now() AS live
                    """,
                    _sha256(code),
                )
                if not row or not row["live"]:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                computed = _b64url(_sha256(verifier))
                if not secrets.compare_digest(computed, row["code_challenge"]):
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                parent_live = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM core.dashboard_sessions s
                          JOIN core.webauthn_credentials c
                            ON c.credential_id = s.credential_id
                         WHERE s.session_hash = $1
                           AND s.revoked_at IS NULL
                           AND c.revoked_at IS NULL
                    )
                    """,
                    bytes(row["parent_session_hash"]),
                )
                if not parent_live:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                await conn.execute("DELETE FROM core.hub_sessions WHERE expires_at <= now()")
                created = await conn.fetchrow(
                    """
                    INSERT INTO core.hub_sessions
                        (handle_hash, parent_session_hash, credential_id,
                         operator_label, created_at, expires_at)
                    VALUES ($1, $2, $3, $4, now(), now() + make_interval(secs => $5))
                    RETURNING created_at, expires_at
                    """,
                    _sha256(handle),
                    bytes(row["parent_session_hash"]),
                    bytes(row["credential_id"]),
                    row["operator_label"],
                    HUB_SESSION_SECONDS,
                )
    except Exception as exc:
        logger.warning("hub code exchange failed: %s", exc)
        return JSONResponse({"error": "authentication storage unavailable"}, status_code=503)
    await _emit_auth_event(
        "hub_signin",
        row["operator_label"],
        credential_id=_b64url(bytes(row["credential_id"])),
    )
    return JSONResponse(
        {
            "handle": handle,
            "operator_label": row["operator_label"],
            "created_at": created["created_at"].isoformat(),
            "expires_at": created["expires_at"].isoformat(),
        },
        headers={"Cache-Control": "no-store"},
    )


async def http_hub_check(request):
    """Report whether a hub session is live. Read-only: nothing slides.

    ``fresh`` is measured from the passkey sign-in that created the parent
    dashboard session (``authenticated_at``), not from the exchange: a hub
    approved nine minutes after that sign-in is fresh for one more minute.
    """
    body, denied = await _hub_internal_request(request)
    if denied:
        return denied
    handle = _hub_handle(body)
    if handle is None:
        return JSONResponse({"ok": False}, status_code=401)
    try:
        db = get_db()
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT h.operator_label, h.created_at, h.expires_at,
                       s.created_at AS authenticated_at,
                       now() - s.created_at <= make_interval(secs => $2) AS fresh
                  FROM core.hub_sessions h
                  JOIN core.dashboard_sessions s
                    ON s.session_hash = h.parent_session_hash
                  JOIN core.webauthn_credentials c
                    ON c.credential_id = h.credential_id
                 WHERE h.handle_hash = $1
                   AND h.revoked_at IS NULL
                   AND h.expires_at > now()
                   AND s.revoked_at IS NULL
                   AND c.revoked_at IS NULL
                """,
                _sha256(handle),
                STEP_UP_TTL_SECONDS,
            )
    except Exception as exc:  # storage unavailable => deny, never fail open
        logger.debug("hub session check failed closed: %s", exc)
        return JSONResponse({"ok": False, "error": "authentication storage unavailable"}, status_code=503)
    if not row:
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse(
        {
            "ok": True,
            "operator_label": row["operator_label"],
            "created_at": row["created_at"].isoformat(),
            "expires_at": row["expires_at"].isoformat(),
            "authenticated_at": row["authenticated_at"].isoformat(),
            "fresh": bool(row["fresh"]),
        },
        headers={"Cache-Control": "no-store"},
    )


async def http_hub_revoke(request):
    """End a hub session (the hub's sign-out)."""
    body, denied = await _hub_internal_request(request)
    if denied:
        return denied
    handle = _hub_handle(body)
    if handle is None:
        return JSONResponse({"error": "invalid handle"}, status_code=400)
    try:
        db = get_db()
        async with db.acquire() as conn:
            label = await conn.fetchval(
                """
                UPDATE core.hub_sessions SET revoked_at = now()
                 WHERE handle_hash = $1 AND revoked_at IS NULL
                 RETURNING operator_label
                """,
                _sha256(handle),
            )
    except Exception as exc:
        logger.warning("hub session revoke failed: %s", exc)
        return JSONResponse({"error": "authentication storage unavailable"}, status_code=503)
    if label is not None:
        await _emit_auth_event("hub_session_revoked", label)
    return JSONResponse({"success": True, "revoked": label is not None})
