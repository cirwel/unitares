"""S1-a — continuity_token retirement, narrowed.

Tests the TTL shrink, ownership_proof_version schema extension, and
deprecation-warning emission
§4.1, §4.3, §4.5.

Reference: S1 plan doc §7 risks require regression coverage for:
- PATH-0-with-expired-token (Part-C invariant preserved under short TTL)
- TTL-boundary behavior (token valid at edge, rejected just past)
- ownership_proof_version surfaces in payload and response
- deprecation warning fires only on cross-instance resume (onboard+token)
- deprecation does NOT fire on intra-session continuity_token use
"""
from __future__ import annotations

import base64
import inspect
import json
import time
from unittest.mock import patch

import pytest


# -----------------------------------------------------------------------------
# 4.1 TTL shrink
# -----------------------------------------------------------------------------


def test_continuity_ttl_is_one_hour():
    """Per S1-a: _CONTINUITY_TTL shrinks from 30 days to 1 hour (3600s).

    This is the operator-approved value. Threshold is a convenience anchor,
    not threat-model-derived — see s1 doc §11.2.
    """
    from src.mcp_handlers.identity import session as session_mod

    assert session_mod._CONTINUITY_TTL == 3600, (
        f"S1-a expects 1h TTL (3600s); got {session_mod._CONTINUITY_TTL}"
    )


def test_token_issued_with_default_ttl_carries_1h_expiry():
    """Newly-minted token's exp claim reflects the shrunk TTL."""
    from src.mcp_handlers.identity.session import create_continuity_token

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        before = int(time.time())
        token = create_continuity_token(
            "11111111-2222-3333-4444-555555555555",
            "agent-111:claude",
        )
        after = int(time.time())

    assert token is not None and token.startswith("v1.")
    _, payload_b64, _ = token.split(".", 2)
    payload = json.loads(_b64url_decode(payload_b64))

    expiry_offset = payload["exp"] - payload["iat"]
    assert expiry_offset == 3600, (
        f"expected 3600s offset, got {expiry_offset}"
    )
    assert before <= payload["iat"] <= after


def test_resolve_rejects_token_past_clock_skew_window():
    """Token valid through clock-skew window; rejected past it.

    S1 doc §7.2 flagged clock-skew near boundary as a new code path under short
    TTL. _CLOCK_SKEW_TOLERANCE (30s, 2026-04-29) gives a small grace zone for
    typical NTP drift, then strict rejection. Two assertions: still valid
    inside the window, rejected just past it.
    """
    from src.mcp_handlers.identity import session as session_mod
    from src.mcp_handlers.identity.session import (
        create_continuity_token,
        resolve_continuity_token,
    )

    tol = session_mod._CLOCK_SKEW_TOLERANCE
    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        token = create_continuity_token(
            "11111111-2222-3333-4444-555555555555",
            "agent-111:claude",
        )
        assert token is not None

        # Token valid now
        assert resolve_continuity_token(token) == "agent-111:claude"

        # Just past old strict boundary (exp + 1) — would have rejected pre-tolerance,
        # NOW resolves. This assertion catches a sign-error or revert of the tolerance
        # constant; without it, `+ _CLOCK_SKEW_TOLERANCE` becoming `- _CLOCK_SKEW_TOLERANCE`
        # would silently pass the rest of this test.
        past_strict = int(time.time()) + 3601
        with patch("src.mcp_handlers.identity.session.time.time", return_value=past_strict):
            assert resolve_continuity_token(token) == "agent-111:claude", (
                "tolerance must keep token resolvable past the old strict exp boundary"
            )

        # Inside skew window (exp + tol - 1) — still valid
        within = int(time.time()) + 3600 + tol - 1
        with patch("src.mcp_handlers.identity.session.time.time", return_value=within):
            assert resolve_continuity_token(token) == "agent-111:claude"

        # Past skew window (exp + tol + 1) — rejected
        past = int(time.time()) + 3600 + tol + 1
        with patch("src.mcp_handlers.identity.session.time.time", return_value=past):
            assert resolve_continuity_token(token) is None


def test_clock_skew_tolerance_constant_value():
    """Pin the tolerance value so silent enlargement requires a code-review touch."""
    from src.mcp_handlers.identity import session as session_mod

    assert session_mod._CLOCK_SKEW_TOLERANCE == 30, (
        f"S1-a expects 30s clock-skew tolerance; got {session_mod._CLOCK_SKEW_TOLERANCE}"
    )
    assert session_mod._CLOCK_SKEW_TOLERANCE < session_mod._CONTINUITY_TTL, (
        "tolerance must be strictly less than TTL or it can swallow whole token validity"
    )


def test_token_expiry_mid_call_resolves_then_rejects():
    """A caller that hits resolve once before exp and once after must see
    accept→reject across the boundary.

    Defends against caching-too-aggressively bugs in resolve_continuity_token.
    Per S1 doc §7.2.
    """
    from src.mcp_handlers.identity.session import (
        create_continuity_token,
        resolve_continuity_token,
        _CLOCK_SKEW_TOLERANCE,
    )

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        token = create_continuity_token(
            "22222222-3333-4444-5555-666666666666",
            "agent-222:claude",
        )
        assert token is not None

        # First call: token fresh, resolves
        assert resolve_continuity_token(token) == "agent-222:claude"

        # Second call later: well past tolerance window, rejects
        later = int(time.time()) + 3600 + _CLOCK_SKEW_TOLERANCE + 60
        with patch("src.mcp_handlers.identity.session.time.time", return_value=later):
            assert resolve_continuity_token(token) is None


def test_per_token_expiry_independence():
    """Two tokens for the same agent_uuid: stale one rejects, fresh one accepts.

    Pins the invariant that expiry is per-token, not per-agent_uuid — defends
    against any future shared-state expiry coupling. NOTE: this is NOT the
    s1 doc §7.2.3 concurrent-possessor race (that requires interleaved resolves
    under wait_for/threading) — concurrent-resolve coverage is left to future
    work; this is a useful ancillary that prevents the wrong-shape regression.
    """
    from src.mcp_handlers.identity.session import (
        create_continuity_token,
        resolve_continuity_token,
        _CLOCK_SKEW_TOLERANCE,
    )

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        agent_uuid = "33333333-4444-5555-6666-777777777777"
        # Old token: minted "an hour and a half ago"
        with patch(
            "src.mcp_handlers.identity.session.time.time",
            return_value=int(time.time()) - 5400,
        ):
            stale = create_continuity_token(agent_uuid, "agent-333:claude")
        # Fresh token: minted now
        fresh = create_continuity_token(agent_uuid, "agent-333:claude")

        assert stale is not None and fresh is not None and stale != fresh
        # Stale rejects (1.5h old, well past 1h + 30s tolerance)
        assert resolve_continuity_token(stale) is None
        # Fresh accepts
        assert resolve_continuity_token(fresh) == "agent-333:claude"


def test_chronicler_shape_old_token_forces_reonboard():
    """Resident-shape regression: a >1h-old continuity_token (e.g. Chronicler
    daily-cron resuming after a long gap) MUST not resolve, forcing the SDK
    fallback to ``onboard(force_new=true, parent_agent_id=<own UUID>)``.

    Operator decision per s1 doc §11.2: 1h TTL acceptable because Chronicler
    re-onboards on wake. This test pins that behavior — if a future TTL change
    or skew-tolerance bump makes >1h tokens resolvable, session-like residents
    would skip the lineage-declared onboard path and re-introduce the silent-
    re-binding pattern S11/S13 retired.

    Scope: this rejection applies to session-like / Chronicler-shape residents
    that resolve via the token path. Substrate-anchored residents under UDS
    (Vigil/Sentinel via S19 M3-v2) pass PATH 0's Part-C ownership gate via
    ``extract_token_agent_uuid`` (which intentionally ignores expiry per Part C
    / PR #42) followed by kernel-attested peer match — neither call exercises
    ``resolve_continuity_token``. See ``test_path0_substrate_does_not_use_resolve_continuity_token``.
    """
    from src.mcp_handlers.identity.session import (
        create_continuity_token,
        resolve_continuity_token,
        _CLOCK_SKEW_TOLERANCE,
    )

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        # Token from "yesterday" (24h old) — Chronicler daily-cron shape.
        with patch(
            "src.mcp_handlers.identity.session.time.time",
            return_value=int(time.time()) - 86400,
        ):
            old_token = create_continuity_token(
                "44444444-5555-6666-7777-888888888888",
                "agent-444:chronicler",
            )

        assert old_token is not None
        # Skew tolerance must not be wide enough to admit a 24h-old token.
        assert _CLOCK_SKEW_TOLERANCE < 86400
        assert resolve_continuity_token(old_token) is None


# -----------------------------------------------------------------------------
# 4.5 ownership_proof_version schema extension
# -----------------------------------------------------------------------------


def test_token_payload_carries_opv_field():
    """JWT payload gains "opv": 1. Forward-compat: future A′/B bump to 2/3."""
    from src.mcp_handlers.identity.session import create_continuity_token

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        token = create_continuity_token(
            "11111111-2222-3333-4444-555555555555",
            "agent-111:claude",
        )

    assert token is not None
    _, payload_b64, _ = token.split(".", 2)
    payload = json.loads(_b64url_decode(payload_b64))
    assert payload.get("opv") == 1, (
        f"expected opv=1 in payload, got {payload!r}"
    )


def test_continuity_support_status_exposes_ownership_proof_version():
    """Diagnostic surface surfaces the version for log consumers + dashboards."""
    from src.mcp_handlers.identity.session import continuity_token_support_status

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        status = continuity_token_support_status()

    assert status["enabled"] is True
    assert status.get("ownership_proof_version") == 1


def test_continuity_support_status_omits_version_when_disabled():
    """No secret = no token, no version — cleanly disabled state."""
    from src.mcp_handlers.identity.session import continuity_token_support_status

    # Clear all three possible secret sources
    with patch.dict(
        "os.environ",
        {
            "UNITARES_CONTINUITY_TOKEN_SECRET": "",
            "UNITARES_HTTP_API_TOKEN": "",
            "UNITARES_API_TOKEN": "",
        },
        clear=False,
    ):
        status = continuity_token_support_status()

    assert status["enabled"] is False
    assert "ownership_proof_version" not in status or status.get("ownership_proof_version") is None


# -----------------------------------------------------------------------------
# 4.3 Deprecation warning emission
# -----------------------------------------------------------------------------


def test_build_token_deprecation_block_for_resume():
    """onboard+token (without force_new) is the retiring cross-instance resume path.

    S1 doc §4.3: grace-period warning fires for this case.
    """
    from src.mcp_handlers.identity.session import build_token_deprecation_block

    block = build_token_deprecation_block(
        used_token_for_resume=True,
        token_issued_at=int(time.time()) - 60,
    )

    assert block is not None
    assert block["field"] == "continuity_token"
    assert block["severity"] == "warning"
    assert "deprecated" in block["message"].lower()
    assert "parent_agent_id" in block["message"]
    assert "force_new=true" in block["message"]
    assert "sunset" in block


def test_no_deprecation_for_non_resume_usage():
    """Intra-session token use (request auth, mid-session identity calls) is NOT deprecated.

    Only the onboard+token cross-instance resume path warns. S1 doc §4.3.
    """
    from src.mcp_handlers.identity.session import build_token_deprecation_block

    assert build_token_deprecation_block(used_token_for_resume=False) is None


# -----------------------------------------------------------------------------
# 4.3 audit event
# -----------------------------------------------------------------------------


def test_audit_log_has_continuity_token_deprecated_accept_method(tmp_path):
    """Audit sink has a typed method for the grace-period event per S1 doc §6."""
    from src.audit_log import AuditLogger

    audit = AuditLogger(log_file=tmp_path / "audit.jsonl")
    assert hasattr(audit, "log_continuity_token_deprecated_accept"), (
        "expected log_continuity_token_deprecated_accept on AuditLog"
    )

    # Method accepts the §6-specified fields and writes a JSONL entry.
    audit.log_continuity_token_deprecated_accept(
        agent_id="agent-xyz",
        caller_channel="claude_code",
        caller_model_type="claude",
        issued_at=int(time.time()) - 600,
        accepted_at=int(time.time()),
        agent_uuid="11111111-2222-3333-4444-555555555555",
    )

    logged = (tmp_path / "audit.jsonl").read_text().strip()
    assert logged, "expected one JSONL entry"
    entry = json.loads(logged.splitlines()[-1])
    assert entry["event_type"] == "continuity_token_deprecated_accept"
    details = entry["details"]
    assert details["caller_channel"] == "claude_code"
    assert details["caller_model_type"] == "claude"
    assert details["agent_uuid"] == "11111111-2222-3333-4444-555555555555"
    assert isinstance(details["lifetime_seconds"], int)
    assert details["lifetime_seconds"] >= 0


# -----------------------------------------------------------------------------
# Part-C invariant regression (§7.2)
# -----------------------------------------------------------------------------


def test_part_c_extract_token_agent_uuid_survives_expiry():
    """extract_token_agent_uuid deliberately ignores exp (Part-C / PR #42).

    Under short TTL this matters more, not less: residents idle past TTL still
    need signature-verification to work for identity lookup. This test pins
    the existing contract — any future refactor that enforces exp here breaks
    Part-C.
    """
    from src.mcp_handlers.identity.session import (
        create_continuity_token,
        extract_token_agent_uuid,
    )

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        token = create_continuity_token(
            "11111111-2222-3333-4444-555555555555",
            "agent-xyz",
        )
        assert token is not None

        # Sim token aged well past 1h TTL
        future = int(time.time()) + 7200
        with patch("src.mcp_handlers.identity.session.time.time", return_value=future):
            # resolve_continuity_token rejects (enforces exp)
            from src.mcp_handlers.identity.session import resolve_continuity_token
            assert resolve_continuity_token(token) is None
            # extract_token_agent_uuid still returns aid (ignores exp — Part C)
            assert extract_token_agent_uuid(token) == "11111111-2222-3333-4444-555555555555"


# -----------------------------------------------------------------------------
# extract_token_iat helper (§6 audit-event dependency)
# -----------------------------------------------------------------------------


def test_extract_token_iat_returns_issued_at():
    """Grace-period audit needs the token's iat claim at accept time.

    extract_token_iat verifies signature (like extract_token_agent_uuid) but
    skips expiry — an expired token still carries an honest iat.
    """
    from src.mcp_handlers.identity.session import (
        create_continuity_token,
        extract_token_iat,
    )

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        before = int(time.time())
        token = create_continuity_token(
            "11111111-2222-3333-4444-555555555555",
            "agent-iat",
        )
        after = int(time.time())
        iat = extract_token_iat(token)

    assert iat is not None
    assert before <= iat <= after


def test_extract_token_iat_rejects_tampered_token():
    """Signature mismatch → None (same trust model as extract_token_agent_uuid)."""
    from src.mcp_handlers.identity.session import extract_token_iat

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        # Construct obviously-malformed token
        assert extract_token_iat("v1.junk.sig") is None
        assert extract_token_iat("") is None
        assert extract_token_iat(None) is None  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# ownership_proof_version surfaces on top-level response (§4.5)
# -----------------------------------------------------------------------------


def test_onboard_response_surfaces_ownership_proof_version_top_level():
    """Dashboard + log consumers can read opv without digging into token payload."""
    from src.services.identity_payloads import build_onboard_response_data
    from src.mcp_handlers.identity.session import continuity_token_support_status

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        support = continuity_token_support_status()

    result = build_onboard_response_data(
        agent_uuid="11111111-2222-3333-4444-555555555555",
        response_agent_id="agent_11111111",
        agent_label=None,
        stable_session_id="agent-111",
        is_new=True,
        force_new=False,
        client_hint="unknown",
        was_archived=False,
        trajectory_result=None,
        parent_agent_id=None,
        thread_context=None,
        verbose=False,
        continuity_source="test",
        continuity_support=support,
        continuity_token="v1.dummy.token",
        system_activity=None,
        tool_mode_info=None,
    )
    assert result.get("ownership_proof_version") == 1


def test_identity_response_surfaces_ownership_proof_version_top_level():
    """Same shape on identity() as onboard() — keep the surface consistent."""
    from src.services.identity_payloads import build_identity_response_data
    from src.mcp_handlers.identity.session import continuity_token_support_status

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        support = continuity_token_support_status()

    result = build_identity_response_data(
        agent_uuid="11111111-2222-3333-4444-555555555555",
        agent_id="agent_11111111",
        display_name=None,
        client_session_id="agent-111",
        continuity_source="test",
        continuity_support=support,
        continuity_token="v1.dummy.token",
        identity_status="active",
        model_type=None,
        resumed=True,
        session_continuity=None,
        verbose=False,
    )
    assert result.get("ownership_proof_version") == 1


def test_response_omits_ownership_proof_version_when_disabled():
    """When token support is off, the field is absent (not None, not 0)."""
    from src.services.identity_payloads import build_onboard_response_data

    support_disabled = {"enabled": False, "secret_source": None}
    result = build_onboard_response_data(
        agent_uuid="11111111-2222-3333-4444-555555555555",
        response_agent_id="agent_11111111",
        agent_label=None,
        stable_session_id="agent-111",
        is_new=True,
        force_new=False,
        client_hint="unknown",
        was_archived=False,
        trajectory_result=None,
        parent_agent_id=None,
        thread_context=None,
        verbose=False,
        continuity_source="test",
        continuity_support=support_disabled,
        continuity_token=None,
        system_activity=None,
        tool_mode_info=None,
    )
    assert "ownership_proof_version" not in result


# -----------------------------------------------------------------------------
# §7.3 bind_session inherits the new short TTL
# -----------------------------------------------------------------------------


def test_bind_session_resolve_path_uses_1h_ttl():
    """bind_session calls resolve_continuity_token which uses _CONTINUITY_TTL.
    S1-a's shrink propagates; §7.3 operator call was let-it-propagate. Verify
    a token past TTL+skew is rejected by the bind_session resolve path —
    same function, same behavior.
    """
    from src.mcp_handlers.identity.session import (
        create_continuity_token,
        resolve_continuity_token,
        _CLOCK_SKEW_TOLERANCE,
    )

    with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
        token = create_continuity_token(
            "11111111-2222-3333-4444-555555555555",
            "agent-bind-test",
        )
        assert token is not None

        # Within TTL — resolves
        assert resolve_continuity_token(token) == "agent-bind-test"

        # Past 1h TTL + skew window — rejected (same gate bind_session hits)
        past_ttl = int(time.time()) + 3600 + _CLOCK_SKEW_TOLERANCE + 1
        with patch("src.mcp_handlers.identity.session.time.time", return_value=past_ttl):
            assert resolve_continuity_token(token) is None


@pytest.mark.asyncio
async def test_bind_session_handler_rejects_token_only_resume_s1c():
    """S1-c: bind_session no longer accepts continuity_token as a bind input.

    Explicit client_session_id binding remains valid. Token-only binding was
    the retired cross-process-instance resume surface.
    """
    from src.mcp_handlers.identity.handlers import handle_bind_session
    from src.mcp_handlers.identity.session import create_continuity_token
    from tests.helpers import parse_result

    with patch.dict(
        "os.environ",
        {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret-s9"},
        clear=False,
    ):
        token = create_continuity_token(
            "11111111-2222-3333-4444-555555555555",
            "agent-s9-bind-test",
        )
        assert token is not None

        result = await handle_bind_session({
            "continuity_token": token,
            "resume": True,
        })

    data = parse_result(result)
    assert data["success"] is False
    assert data["status"] == "continuity_token_resume_rejected"
    assert data["tool"] == "bind_session"
    assert data["recovery"]["reason"] == "continuity_token_resume_retired"


@pytest.mark.asyncio
async def test_onboard_rejects_token_resume_s1c():
    """S1-c: onboard(token) must fail closed; lineage declaration replaces it."""
    from unittest.mock import AsyncMock
    from src.mcp_handlers.identity.handlers import handle_onboard_v2
    from src.mcp_handlers.identity.session import create_continuity_token
    from tests.helpers import parse_result

    with patch.dict(
        "os.environ",
        {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret-s1c"},
        clear=False,
    ):
        token = create_continuity_token(
            "22222222-3333-4444-5555-666666666666",
            "agent-s1c-onboard",
        )
        assert token is not None
        with patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new=AsyncMock(return_value="s1c-onboard-session"),
        ):
            result = await handle_onboard_v2({
                "continuity_token": token,
                "client_hint": "test",
            })

    data = parse_result(result)
    assert data["success"] is False
    assert data["status"] == "continuity_token_resume_rejected"
    assert data["tool"] == "onboard"


@pytest.mark.asyncio
async def test_identity_token_only_resume_rejected_but_path0_preserved_s1c():
    """Token-only identity resume is retired; PATH 0 ownership proof remains."""
    from unittest.mock import AsyncMock
    from src.mcp_handlers.identity.handlers import handle_identity_adapter
    from src.mcp_handlers.identity.session import create_continuity_token
    from tests.helpers import parse_result

    token_uuid = "33333333-4444-5555-6666-777777777777"
    with patch.dict(
        "os.environ",
        {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret-s1c"},
        clear=False,
    ):
        token = create_continuity_token(token_uuid, "agent-s1c-identity")
        assert token is not None
        with patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new=AsyncMock(return_value="s1c-identity-session"),
        ):
            result = await handle_identity_adapter({
                "continuity_token": token,
            })

    data = parse_result(result)
    assert data["success"] is False
    assert data["status"] == "continuity_token_resume_rejected"
    assert data["tool"] == "identity"

    src = inspect.getsource(handle_identity_adapter)
    assert "_try_resume_by_agent_uuid_direct" in src
    assert "_continuity_token_resume_rejected" in src


# -----------------------------------------------------------------------------
# Substrate-anchored PATH 0 invariance under TTL/skew changes (S1-a + S19)
# -----------------------------------------------------------------------------


def test_path0_substrate_does_not_use_resolve_continuity_token():
    """Substrate-anchored residents (Vigil/Sentinel via S19 M3-v2) must resolve
    independently of ``resolve_continuity_token``'s expiry contract.

    PATH 0's Part-C ownership gate uses ``extract_token_agent_uuid`` (ignores
    expiry per PR #42) and the substrate-attestation gate uses ``peer_pid``
    + ``verify_substrate_at_resume``. Neither path exercises
    ``resolve_continuity_token``. If a future refactor wires
    ``resolve_continuity_token`` into ``_try_resume_by_agent_uuid_direct``,
    the new TTL+skew contract would silently make substrate residents fail
    over to token-resolve rejection — regressing S19's substrate-attestation
    closure of the Hermes anchor-leak class. This test pins the structural
    invariant via static source inspection.
    """
    import inspect
    from src.mcp_handlers.identity import handlers

    src = inspect.getsource(handlers._try_resume_by_agent_uuid_direct)
    assert "resolve_continuity_token" not in src, (
        "PATH 0 must NOT call resolve_continuity_token — substrate-anchored "
        "residents would silently regress under TTL/clock-skew changes. "
        "See S1-a + S19 interaction note in the Chronicler-shape test."
    )
    # Positive contract: PATH 0 still uses extract_token_agent_uuid for Part-C
    # ownership proof (which intentionally ignores expiry), and the S19
    # substrate gate via verify_substrate_at_resume.
    assert "extract_token_agent_uuid" in src
    assert "verify_substrate_at_resume" in src


# -----------------------------------------------------------------------------
# §4.3 deprecation wiring at identity() and bind_session() (S1-a, 2026-04-29)
# -----------------------------------------------------------------------------


def test_emit_helper_appends_deprecation_block_and_audits(tmp_path, monkeypatch):
    """Helper mutates response_dict["deprecations"] and writes one audit entry.

    Pins the contract that the three handler call sites (onboard, identity,
    bind_session) all rely on. Ontology: only fires when used_token_for_resume
    is True.
    """
    from src.mcp_handlers.identity.handlers import _emit_continuity_token_deprecation
    from src.mcp_handlers.identity.session import create_continuity_token
    from src import audit_log as audit_mod

    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "test-secret")
    audit = audit_mod.AuditLogger(log_file=tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit_mod, "audit_logger", audit)

    token = create_continuity_token(
        "55555555-6666-7777-8888-999999999999",
        "agent-555:claude",
    )

    response: dict = {}
    _emit_continuity_token_deprecation(
        response_dict=response,
        used_token_for_resume=True,
        token_str=token,
        agent_uuid="55555555-6666-7777-8888-999999999999",
        response_agent_id="agent-555:claude",
        client_hint="claude_code",
        model_type="claude",
    )

    assert "deprecations" in response and len(response["deprecations"]) == 1
    block = response["deprecations"][0]
    assert block["field"] == "continuity_token"
    assert block["severity"] == "warning"

    logged = (tmp_path / "audit.jsonl").read_text().strip()
    assert logged, "expected one audit entry"
    entry = json.loads(logged.splitlines()[-1])
    assert entry["event_type"] == "continuity_token_deprecated_accept"


def test_emit_helper_noop_when_not_resume(monkeypatch):
    """used_token_for_resume=False → no mutation, no audit. Intra-session use is NOT
    the deprecating surface."""
    from src.mcp_handlers.identity.handlers import _emit_continuity_token_deprecation

    response: dict = {"existing": "value"}
    _emit_continuity_token_deprecation(
        response_dict=response,
        used_token_for_resume=False,
        token_str="v1.aaa.bbb",
        agent_uuid="x",
        response_agent_id="y",
    )
    assert response == {"existing": "value"}


def test_emit_helper_logs_s1d_false_observation_once(monkeypatch, caplog):
    """S1-d telemetry: the False (non-deprecating) path now positively records
    that the deprecation surface was reached with used_token_for_resume=False,
    once per process — so 'observed False' can be confirmed rather than only
    inferred from the absence of accept audits. The audit/mutation behavior is
    unchanged (still a no-op)."""
    import logging
    from src.mcp_handlers.identity import handlers as H

    # Reset the per-process latch so the assertion is deterministic.
    monkeypatch.setattr(H, "_s1d_false_reached_logged", False)

    response: dict = {"existing": "value"}
    with caplog.at_level(logging.INFO):
        H._emit_continuity_token_deprecation(
            response_dict=response,
            used_token_for_resume=False,
            token_str="v1.aaa.bbb",
            agent_uuid="x",
            response_agent_id="y",
        )
        # Second call in the same process must NOT log again (bounded noise).
        H._emit_continuity_token_deprecation(
            response_dict=response,
            used_token_for_resume=False,
            token_str="v1.aaa.bbb",
            agent_uuid="x",
            response_agent_id="y",
        )

    s1d_lines = [r for r in caplog.records if "[S1-d]" in r.getMessage()]
    assert len(s1d_lines) == 1, "telemetry must log exactly once per process"
    assert "used_token_for_resume=False" in s1d_lines[0].getMessage()
    # No mutation on the False path — behavior unchanged.
    assert response == {"existing": "value"}

def test_emit_helper_swallows_audit_failure(tmp_path, monkeypatch, caplog):
    """Audit emission failures must not propagate to the request path."""
    from src.mcp_handlers.identity.handlers import _emit_continuity_token_deprecation
    from src import audit_log as audit_mod

    class _Boom:
        def log_continuity_token_deprecated_accept(self, **kwargs):
            raise RuntimeError("disk full")

    monkeypatch.setattr(audit_mod, "audit_logger", _Boom())

    response: dict = {}
    # Must not raise even with the audit logger throwing
    _emit_continuity_token_deprecation(
        response_dict=response,
        used_token_for_resume=True,
        token_str=None,
        agent_uuid="x",
        response_agent_id="y",
    )
    # Dep block still added (response surface independent of audit sink)
    assert "deprecations" in response


def test_handle_identity_adapter_s1c_rejection_wiring_present():
    """Static contract: token-only identity resume hits the S1-c rejection gate."""
    import inspect
    from src.mcp_handlers.identity import handlers

    src = inspect.getsource(handlers.handle_identity_adapter)
    assert "_continuity_token_resume_rejected" in src
    assert 'tool="identity"' in src


def test_handle_bind_session_s1c_rejection_wiring_present():
    """Static contract: bind_session rejects token-only binding."""
    import inspect
    from src.mcp_handlers.identity import handlers

    src = inspect.getsource(handlers.handle_bind_session)
    assert "_continuity_token_resume_rejected" in src
    assert 'tool="bind_session"' in src


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


# -----------------------------------------------------------------------------
# §7 governed-effect strong-tier re-certification (recertify_strong_tier)
# -----------------------------------------------------------------------------
# Unit-level proof for the encapsulated §7 gate: "fresh HMAC AND aid == claimed
# proposer." Single-sourced here so the invariant is tested as a function, not
# only as an HTTP shape (see tests/test_http_api_effect_veto.py for the
# endpoint-level proof).

_S7_SECRET = {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret-s7"}
_S7_AID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_recertify_strong_tier_accepts_fresh_matching_token():
    from src.mcp_handlers.identity import session as session_mod

    with patch.dict("os.environ", _S7_SECRET, clear=False):
        token = session_mod.create_continuity_token(_S7_AID, "agent-aaa:claude")
        assert session_mod.recertify_strong_tier(token, _S7_AID) is True


def test_recertify_strong_tier_rejects_missing_token_or_proposer():
    from src.mcp_handlers.identity import session as session_mod

    with patch.dict("os.environ", _S7_SECRET, clear=False):
        token = session_mod.create_continuity_token(_S7_AID, "agent-aaa:claude")
        assert session_mod.recertify_strong_tier(None, _S7_AID) is False
        assert session_mod.recertify_strong_tier("", _S7_AID) is False
        assert session_mod.recertify_strong_tier(token, None) is False
        assert session_mod.recertify_strong_tier(token, "") is False
        # non-str inputs must not raise
        assert session_mod.recertify_strong_tier(123, _S7_AID) is False
        assert session_mod.recertify_strong_tier(token, 123) is False


def test_recertify_strong_tier_rejects_aid_mismatch():
    """A perfectly valid token for identity X cannot re-certify proposer Y."""
    from src.mcp_handlers.identity import session as session_mod

    with patch.dict("os.environ", _S7_SECRET, clear=False):
        token = session_mod.create_continuity_token(_S7_AID, "agent-aaa:claude")
        assert session_mod.recertify_strong_tier(token, "ffffffff-0000-0000-0000-000000000000") is False


def test_recertify_strong_tier_rejects_expired_token():
    """Freshness IS required (irreversible spawn) — an expired token is refused
    even though its signature and aid are valid."""
    from src.mcp_handlers.identity import session as session_mod

    with patch.dict("os.environ", _S7_SECRET, clear=False):
        with patch.object(session_mod.time, "time", return_value=int(time.time()) - 7200):
            stale = session_mod.create_continuity_token(_S7_AID, "agent-aaa:claude")
        assert session_mod.recertify_strong_tier(stale, _S7_AID) is False


def test_recertify_strong_tier_rejects_tampered_signature():
    from src.mcp_handlers.identity import session as session_mod

    with patch.dict("os.environ", _S7_SECRET, clear=False):
        token = session_mod.create_continuity_token(_S7_AID, "agent-aaa:claude")
        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        assert session_mod.recertify_strong_tier(tampered, _S7_AID) is False


def test_recertify_strong_tier_fails_closed_without_secret():
    from src.mcp_handlers.identity import session as session_mod

    with patch.dict("os.environ", _S7_SECRET, clear=False):
        token = session_mod.create_continuity_token(_S7_AID, "agent-aaa:claude")
    # secret removed → no token can verify → fail closed
    with patch.dict("os.environ", {}, clear=True):
        assert session_mod.recertify_strong_tier(token, _S7_AID) is False
