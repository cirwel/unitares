"""#1319: a hijack-guard-rejected resume must fail closed, not mint a phantom.

2026-07-01 dogfood incident: a claude.ai mobile client's egress IP rotated
mid-session, so the strict fingerprint guard correctly rejected the PATH 1
resume; the caller's continuity_token was truncated and could not rescue it.
The guard's `resume = False` flip then disarmed BOTH the PATH 2 resume and
the S21-a fail-close (each keyed on `resume`), so the call fell through to a
PATH 3 mint — binding a record_result write to a phantom uuid4 with
success:true (audit.outcome_events 524032fd, agent 2f048f03 which exists
nowhere in the registry).

These tests pin the fix:
  - resolution: a resume flipped off by the hijack guard returns the typed
    failure `resume_rejected_hijack_guard` instead of reaching PATH 3.
  - middleware: under STRICT_IDENTITY_REQUIRED, that failure becomes the #425
    typed refusal (no auto-mint) carrying the rejection reason and, when a
    presented continuity_token failed verification, saying so; non-strict
    deployments keep the legible dispatch_auto_mint retry.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.middleware import resolve_identity, DispatchContext


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("UNITARES_SESSION_FINGERPRINT_CHECK", raising=False)
    monkeypatch.delenv("STRICT_IDENTITY_REQUIRED", raising=False)
    yield


def _signals_with_fp(fp: str):
    sig = MagicMock()
    sig.ip_ua_fingerprint = fp
    return sig


# ─── Resolution layer ──────────────────────────────────────────────────────


class TestResolutionFailsClosedOnHijackRejection:
    @pytest.mark.asyncio
    async def test_strict_fingerprint_mismatch_returns_typed_failure(self, monkeypatch):
        """The dogfood repro: strict fp mismatch + resume → typed failure, no mint."""
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "strict")

        from src.mcp_handlers.identity import resolution as res

        agent_uuid = "33333333-4444-4555-8666-777777777777"
        cached_payload = {
            "agent_id": agent_uuid,
            "display_agent_id": "Claude_mobile",
            "bind_ip_ua": "ip-160.79.106.34:ua-mobile",
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        fake_db = MagicMock()
        fake_db.get_session = AsyncMock(return_value=None)

        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals_with_fp("ip-160.79.106.36:ua-mobile"),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster,
        ), patch.object(
            res, "get_db", return_value=fake_db,
        ):
            result = await res.resolve_session_identity(
                session_key="agent-333333334444",
                resume=True,
                persist=False,
            )

        assert result.get("resume_failed") is True
        assert result.get("error") == "resume_rejected_hijack_guard"
        assert result.get("reason") == "fingerprint_mismatch"
        # The phantom-mint signature of the incident: created=True with a
        # fresh agent_uuid. Neither may appear.
        assert not result.get("created")
        assert "agent_uuid" not in result

    @pytest.mark.asyncio
    async def test_force_new_still_mints_under_strict_fp_mode(self, monkeypatch):
        """force_new callers never trip the guard — ordinary fresh onboards work."""
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "strict")

        from src.mcp_handlers.identity import resolution as res

        with patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals_with_fp("ip-anything:ua-anything"),
        ):
            result = await res.resolve_session_identity(
                session_key="agent-fresh-onboard",
                force_new=True,
                persist=False,
            )

        assert result.get("created") is True
        assert result.get("agent_uuid")

    @pytest.mark.asyncio
    async def test_log_mode_mismatch_still_resumes(self, monkeypatch):
        """Non-strict fp mode is untouched: mismatch logs, resume proceeds."""
        monkeypatch.setenv("UNITARES_SESSION_FINGERPRINT_CHECK", "log")

        from src.mcp_handlers.identity import resolution as res

        agent_uuid = "44444444-5555-4666-8777-888888888888"
        cached_payload = {
            "agent_id": agent_uuid,
            "display_agent_id": "Claude_log_mode",
            "bind_ip_ua": "ip-old:ua-old",
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=cached_payload)

        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()

        with patch.object(res, "_get_redis", return_value=fake_redis), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Claude_log_mode"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            new=AsyncMock(return_value="active"),
        ), patch(
            "src.mcp_handlers.identity.resolution._soft_verify_trajectory",
            new=AsyncMock(return_value={"verified": True}),
        ), patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=_signals_with_fp("ip-new:ua-new"),
        ), patch(
            "src.mcp_handlers.identity.handlers._broadcaster",
            return_value=broadcaster,
        ):
            result = await res.resolve_session_identity(
                session_key="agent-444444445555",
                resume=True,
            )

        assert result.get("agent_uuid") == agent_uuid
        assert result.get("source") == "redis"


# ─── Middleware layer ──────────────────────────────────────────────────────


_HIJACK_FAILURE = {
    "resume_failed": True,
    "error": "resume_rejected_hijack_guard",
    "reason": "fingerprint_mismatch",
    "session_key": "agent-hijacked",
}


def _middleware_patches(resolve_mock, mock_db):
    return [
        patch("src.mcp_handlers.context.get_session_signals", return_value=None),
        patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new_callable=AsyncMock,
            return_value="agent-hijacked",
        ),
        patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            resolve_mock,
        ),
        patch("src.mcp_handlers.context.set_session_context", return_value=MagicMock()),
        patch("src.db.get_db", return_value=mock_db),
    ]


def _refusal_payload(result):
    """Extract the JSON payload from a short-circuit success_response return."""
    assert not isinstance(result, tuple), (
        f"Expected a short-circuit refusal response, got dispatch tuple: {result!r}"
    )
    return json.loads(result[0].text)


class TestMiddlewareHijackGuardRefusal:
    @pytest.mark.asyncio
    async def test_strict_refuses_write_tool_no_auto_mint(self, monkeypatch):
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")

        resolve_mock = AsyncMock(return_value=dict(_HIJACK_FAILURE))
        mock_db = AsyncMock()
        patches = _middleware_patches(resolve_mock, mock_db)
        for p in patches:
            p.start()
        try:
            ctx = DispatchContext()
            result = await resolve_identity(
                "process_agent_update",
                {"client_session_id": "agent-hijacked"},
                ctx,
            )
        finally:
            for p in reversed(patches):
                p.stop()

        # No auto-mint retry: exactly one resolve call.
        assert resolve_mock.await_count == 1
        payload = _refusal_payload(result)
        assert payload.get("status") == "identity_required"
        surface = payload.get("surface_context") or {}
        assert surface.get("resume_rejected_reason") == "fingerprint_mismatch"
        assert "hijack guard" in (payload.get("hint") or "")

    @pytest.mark.asyncio
    async def test_strict_refusal_discloses_invalid_token(self, monkeypatch):
        """A presented-but-unverifiable continuity_token is named in the refusal."""
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")

        resolve_mock = AsyncMock(return_value=dict(_HIJACK_FAILURE))
        mock_db = AsyncMock()
        patches = _middleware_patches(resolve_mock, mock_db)
        for p in patches:
            p.start()
        try:
            ctx = DispatchContext()
            result = await resolve_identity(
                "process_agent_update",
                {
                    "client_session_id": "agent-hijacked",
                    # Truncated tail — extract_token_agent_uuid_safe → None,
                    # same shape as the incident.
                    "continuity_token": "v1.eyJhaWQiOiJ0cnVuY2F0ZWQifQ.dGr",
                },
                ctx,
            )
        finally:
            for p in reversed(patches):
                p.stop()

        payload = _refusal_payload(result)
        surface = payload.get("surface_context") or {}
        assert surface.get("continuity_token_invalid") is True
        assert "failed" in (payload.get("hint") or "").lower()

    @pytest.mark.asyncio
    async def test_non_strict_keeps_auto_mint_retry(self, monkeypatch):
        """Strict off: behavior parity with session_resolve_miss — legible retry."""
        monkeypatch.delenv("STRICT_IDENTITY_REQUIRED", raising=False)

        minted = {
            "agent_uuid": "99999999-8888-4777-8666-555555555555",
            "created": True,
            "persisted": False,
        }
        resolve_mock = AsyncMock(side_effect=[dict(_HIJACK_FAILURE), minted])
        mock_db = AsyncMock()
        patches = _middleware_patches(resolve_mock, mock_db)
        for p in patches:
            p.start()
        try:
            ctx = DispatchContext()
            result = await resolve_identity(
                "process_agent_update",
                {"client_session_id": "agent-hijacked"},
                ctx,
            )
        finally:
            for p in reversed(patches):
                p.stop()

        assert resolve_mock.await_count == 2
        retry_kwargs = resolve_mock.await_args_list[1].kwargs
        assert retry_kwargs.get("force_new") is True
        assert retry_kwargs.get("spawn_reason") == "dispatch_auto_mint"
        assert isinstance(result, tuple), "Non-strict dispatch should proceed"


class _RaisesOnceOnCreated(dict):
    """A resolver result that binds normally, then raises the first time
    ``created`` is read, and reads normally after that."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._raised = False

    def get(self, key, default=None):
        if key == "created" and not self._raised:
            self._raised = True
            raise RuntimeError("bookkeeping failed after binding")
        return super().get(key, default)


class TestMiddlewareResolverExceptionFailsClosed:
    """A resolver that raises must not open the gate a resolver miss closes.

    The typed refusal above runs only on a RETURNED session_resolve_miss or
    hijack rejection. Until 2026-09-13 an exception skipped it: the middleware
    logged at debug, continued with no bound identity, and ran the handler, so
    knowledge(note) wrote under a derived anonymous writer id with
    STRICT_IDENTITY_REQUIRED on.
    """

    @staticmethod
    async def _dispatch(name, arguments, resolve_mock):
        patches = _middleware_patches(resolve_mock, AsyncMock())
        for p in patches:
            p.start()
        try:
            ctx = DispatchContext()
            result = await resolve_identity(name, arguments, ctx)
        finally:
            for p in reversed(patches):
                p.stop()
        return result, ctx

    @pytest.mark.parametrize(
        "resolver_outcome",
        [
            pytest.param({}, id="empty-result"),
            pytest.param(
                {"error": "identity_store_unavailable"},
                id="error-without-resume-failed",
            ),
            pytest.param(
                {"agent_uuid": "", "created": False, "persisted": False},
                id="empty-agent-uuid",
            ),
            pytest.param(
                RuntimeError("identity store unavailable"),
                id="resolver-exception",
            ),
            pytest.param(
                {"resume_failed": True, "error": "session_resolve_miss"},
                id="known-resume-failure",
            ),
            pytest.param(
                {"resume_failed": True, "error": "future_refusal"},
                id="hard-resume-refusal",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_strict_write_pipeline_requires_a_truthy_binding(
        self, monkeypatch, resolver_outcome
    ):
        """Every resolver failure shape stops the real dispatch pipeline."""
        from src.mcp_handlers import dispatch_tool

        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        handler = AsyncMock(return_value=[])
        if isinstance(resolver_outcome, Exception):
            resolve_mock = AsyncMock(side_effect=resolver_outcome)
        else:
            resolve_mock = AsyncMock(return_value=dict(resolver_outcome))

        with patch(
            "src.mcp_handlers.context.get_session_signals", return_value=None
        ), patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new=AsyncMock(return_value="agent-strict-final-guard"),
        ), patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            resolve_mock,
        ), patch.dict(
            "src.mcp_handlers.TOOL_HANDLERS", {"knowledge": handler}
        ):
            result = await dispatch_tool(
                "knowledge",
                {"action": "note", "content": "strict final-guard probe"},
            )

        assert _refusal_payload(result).get("status") == "identity_required"
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_strict_refuses_write_when_resolver_raises(self, monkeypatch):
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        resolve_mock = AsyncMock(side_effect=RuntimeError("identity store unavailable"))

        result, _ = await self._dispatch(
            "knowledge", {"action": "note", "summary": "probe"}, resolve_mock
        )

        payload = _refusal_payload(result)
        assert payload.get("status") == "identity_required"
        assert (payload.get("surface_context") or {}).get("identity_resolution") == "failed"
        assert "handler did not run" in (payload.get("hint") or "")
        assert "identity store unavailable" not in json.dumps(payload), (
            "the refusal must not echo the resolver's exception text"
        )
        # A server-side failure is not a missing identity: steering the caller to
        # onboard would split its work from the identity it already has.
        calls = [option.get("call", "") for option in payload.get("safe_options") or []]
        assert calls and not any("onboard" in call for call in calls), calls

    @pytest.mark.asyncio
    async def test_strict_ignores_a_cached_binding_with_no_identity(self, monkeypatch):
        """A sticky binding with an empty agent_uuid must not bind and dispatch.

        Its early return used to bind the empty value and run the handler
        unbound. It now falls through to resolution, which refuses under strict.
        """
        from types import SimpleNamespace

        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        empty = SimpleNamespace(agent_uuid="", source="mcp", session_key="agent-empty")
        consult = AsyncMock(
            return_value=SimpleNamespace(binding=empty, transport_key="transport-empty")
        )
        resolve_mock = AsyncMock(
            return_value={"resume_failed": True, "error": "session_resolve_miss"}
        )

        with patch(
            "src.mcp_handlers.middleware.identity_step.consult_sticky_binding", consult
        ):
            result, ctx = await self._dispatch(
                "knowledge", {"action": "note", "summary": "probe"}, resolve_mock
            )

        assert resolve_mock.await_count == 1, "an empty binding must fall through to resolution"
        assert _refusal_payload(result).get("status") == "identity_required"
        assert not ctx.bound_agent_id

    @pytest.mark.asyncio
    async def test_strict_lets_a_read_through_when_resolver_raises(self, monkeypatch):
        """A read may run unbound, so a failed resolution does not refuse it.

        client_session_id is identity proof, so the call skips the early
        unbound-read shortcut and reaches resolution, which is the path under test.
        """
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        resolve_mock = AsyncMock(side_effect=RuntimeError("identity store unavailable"))

        result, ctx = await self._dispatch(
            "knowledge",
            {"action": "search", "query": "x", "client_session_id": "agent-reader"},
            resolve_mock,
        )

        assert resolve_mock.await_count == 1, "the read must reach resolution"
        assert isinstance(result, tuple), "a read proceeds when resolution raises"
        assert ctx.bound_agent_id is None

    @pytest.mark.asyncio
    async def test_non_strict_still_continues_unbound(self, monkeypatch):
        monkeypatch.delenv("STRICT_IDENTITY_REQUIRED", raising=False)
        resolve_mock = AsyncMock(side_effect=RuntimeError("identity store unavailable"))

        result, ctx = await self._dispatch(
            "knowledge", {"action": "note", "summary": "probe"}, resolve_mock
        )

        assert isinstance(result, tuple), "non-strict behavior is unchanged"
        assert ctx.bound_agent_id is None

    @pytest.mark.asyncio
    async def test_strict_keeps_an_identity_bound_before_the_failure(self, monkeypatch):
        """An exception after binding leaves an attributed call; it is not refused.

        The X-Agent-Id recovery step is stubbed to return the bound identity so
        the result's first read of ``created`` happens after binding, in the
        ephemeral-marking step, which is where this fake raises. This pins the
        truthy-binding half of the refusal condition: without it, every
        exception under strict mode would refuse, bound or not.
        """
        monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
        bound = "12121212-3434-4565-8787-909090909090"
        resolve_mock = AsyncMock(return_value=_RaisesOnceOnCreated(agent_uuid=bound))

        with patch(
            "src.mcp_handlers.middleware.identity_step._maybe_recover_via_x_agent_id",
            AsyncMock(return_value=bound),
        ):
            result, ctx = await self._dispatch(
                "knowledge", {"action": "note", "summary": "probe"}, resolve_mock
            )

        assert isinstance(result, tuple), "a bound call proceeds"
        assert ctx.bound_agent_id == bound
