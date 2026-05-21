"""
Regression tests for S21-a (session resolution bypass — stop the bleed).

Incident 2026-04-27: explicit `client_session_id` was silently overwritten in
Redis by every PATH 3 ghost-mint, producing a 95.1%/30d fleet-wide ghost-fork
rate. .

Three regression assertions:
  T1  PATH 3 mint must not overwrite an existing in-memory session binding
      that maps to a different agent_uuid.
  T2  PATH 3 mint must not overwrite an existing Redis session binding that
      maps to a different agent_uuid (raw-redis path).
  T3  PATH 2 must fail-closed when resume=True and core.sessions has no row,
      returning resume_failed instead of silently minting via PATH 3.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_no_session():
    db = AsyncMock()
    db.init = AsyncMock()
    db.get_session = AsyncMock(return_value=None)
    db.get_identity = AsyncMock(return_value=None)
    db.get_agent = AsyncMock(return_value=None)
    db.get_agent_label = AsyncMock(return_value=None)
    db.find_agent_by_label = AsyncMock(return_value=None)
    db.upsert_agent = AsyncMock()
    db.upsert_identity = AsyncMock()
    db.create_session = AsyncMock()
    db.update_session_activity = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# T1 / T2 — _cache_session must not overwrite a different live binding
# ---------------------------------------------------------------------------

class TestS21AOverwriteProtection:

    @pytest.mark.asyncio
    async def test_path3_mint_does_not_overwrite_inmemory_binding(self):
        """T1: PATH 3 mint must not overwrite a different in-memory binding.

        Setup: _session_identities already has session_key X bound to legit
        UUID A. A PATH 3 mint fires for the same session_key with fresh UUID
        B (mint_guard=True). After the call, _session_identities[X] must
        still bind to A.
        """
        from src.mcp_handlers.identity.persistence import _cache_session

        legit_uuid = "11111111-1111-1111-1111-111111111111"
        ghost_uuid = "22222222-2222-2222-2222-222222222222"
        session_key = "agent-aaaaaaaaaaaa"

        in_memory = {
            session_key: {
                "bound_agent_id": legit_uuid,
                "agent_uuid": legit_uuid,
                "public_agent_id": legit_uuid,
                "agent_label": "legit-bot",
                "bind_count": 1,
            }
        }

        with patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.identity.shared._session_identities", in_memory):
            await _cache_session(
                session_key, ghost_uuid,
                display_agent_id=ghost_uuid,
                mint_guard=True,
            )

        # Legitimate binding survives
        assert in_memory[session_key]["bound_agent_id"] == legit_uuid
        assert in_memory[session_key]["agent_uuid"] == legit_uuid

    @pytest.mark.asyncio
    async def test_corrective_write_still_overwrites_inmemory_binding(self):
        """Sanity: corrective writes (mint_guard=False) still overwrite.

        PATH 2 / PATH 2.8 / set_agent_label callers don't pass mint_guard,
        so they retain authoritative-overwrite behavior. Without this, the
        guard would also block the recovery path that resolves a stale
        Redis ghost via PostgreSQL fall-through.
        """
        from src.mcp_handlers.identity.persistence import _cache_session

        ghost_uuid = "22222222-2222-2222-2222-222222222222"
        legit_uuid = "11111111-1111-1111-1111-111111111111"
        session_key = "agent-bbbbbbbbbbbb"

        in_memory = {
            session_key: {
                "bound_agent_id": ghost_uuid,
                "agent_uuid": ghost_uuid,
                "public_agent_id": ghost_uuid,
                "bind_count": 1,
            }
        }

        with patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.identity.shared._session_identities", in_memory):
            await _cache_session(session_key, legit_uuid)  # default mint_guard=False

        # Authoritative correction overwrites
        assert in_memory[session_key]["bound_agent_id"] == legit_uuid

    @pytest.mark.asyncio
    async def test_path3_mint_does_not_overwrite_redis_binding(self):
        """T2: PATH 3 mint must not overwrite a different Redis binding.

        Raw Redis returns an existing JSON binding for session_key X with
        agent_id = legit UUID A. A PATH 3 mint fires with fresh UUID B and
        mint_guard=True. The setex (overwrite) must not be called.
        """
        from src.mcp_handlers.identity.persistence import _cache_session

        legit_uuid = "11111111-1111-1111-1111-111111111111"
        ghost_uuid = "22222222-2222-2222-2222-222222222222"
        session_key = "agent-cccccccccccc"

        existing_payload = json.dumps({
            "agent_id": legit_uuid,
            "display_agent_id": legit_uuid,
            "bound_at": "2026-04-27T03:00:00+00:00",
            "trajectory_required": False,
        })
        raw_redis = AsyncMock()
        raw_redis.get = AsyncMock(return_value=existing_payload)
        raw_redis.setex = AsyncMock()

        async def _get_raw():
            return raw_redis

        session_cache = AsyncMock()
        session_cache.bind = AsyncMock()

        in_memory = {}  # In-memory empty so the in-memory guard does not short-circuit.

        with patch("src.mcp_handlers.identity.persistence._redis_cache", session_cache), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.identity.shared._session_identities", in_memory):
            await _cache_session(
                session_key, ghost_uuid,
                # display_agent_id != agent_uuid forces the raw-redis branch
                display_agent_id="ghost-display-id",
                mint_guard=True,
            )

        # The Redis slot is unchanged — no overwrite occurred.
        raw_redis.setex.assert_not_called()
        session_cache.bind.assert_not_called()


# ---------------------------------------------------------------------------
# T3 — PATH 2 fail-closed when resume=True and PG has no row
# ---------------------------------------------------------------------------

class TestS21APath2FailClosed:

    @pytest.mark.asyncio
    async def test_resume_true_pg_miss_returns_resume_failed(self):
        """T3: resume=True + Redis miss + PG miss → resume_failed (no mint).

        Today this falls through to PATH 3 and silently mints a ghost,
        producing the 95% ghost-fork rate. After S21-a, the caller gets a
        resume_failed result and can decide whether to mint via force_new.
        """
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        db = _make_db_no_session()

        with patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=db), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=db), \
             patch("src.audit_log.audit_logger.log_session_resolve_miss_observed") as log_miss:
            result = await resolve_session_identity(
                session_key="agent-no-row-test",
                resume=True,
                client_hint="codex",
                model_type="gpt-5-codex",
            )

        assert result.get("resume_failed") is True, (
            f"expected resume_failed, got {result!r}"
        )
        assert result.get("error") == "session_resolve_miss", (
            f"expected error=session_resolve_miss, got {result.get('error')!r}"
        )
        # No silent mint
        assert result.get("created") is not True
        assert not result.get("agent_uuid")
        log_miss.assert_called_once()
        miss_kwargs = log_miss.call_args.kwargs
        assert miss_kwargs["session_key"] == "agent-no-row-test"
        assert miss_kwargs["reason"] == "pg_session_missing"
        assert miss_kwargs["resume"] is True
        assert miss_kwargs["force_new"] is False
        assert miss_kwargs["token_agent_uuid_present"] is False
        assert miss_kwargs["client_hint"] == "codex"
        assert miss_kwargs["model_type"] == "gpt-5-codex"

    @pytest.mark.asyncio
    async def test_resume_true_pg_exception_returns_resume_failed(self):
        """T3b: resume=True + PG exception → resume_failed (no PATH 3 mint).

        Anyio-asyncio deadlocks (CLAUDE.md) and other PG hiccups raise from
        db.get_session(). The pre-S21-a logger.debug swallowed the failure
        silently and PATH 3 minted. After S21-a the failure is logged at
        warning level *and* the call returns resume_failed.
        """
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        db = AsyncMock()
        db.init = AsyncMock()
        db.get_session = AsyncMock(side_effect=Exception("simulated PG hiccup"))
        db.find_agent_by_label = AsyncMock(return_value=None)

        with patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=db), \
             patch("src.audit_log.audit_logger.log_session_resolve_miss_observed") as log_miss:
            result = await resolve_session_identity(
                session_key="agent-pg-blip",
                resume=True,
            )

        assert result.get("resume_failed") is True
        assert result.get("error") == "session_resolve_miss"
        assert result.get("created") is not True
        log_miss.assert_called_once()
        assert log_miss.call_args.kwargs["session_key"] == "agent-pg-blip"
        assert log_miss.call_args.kwargs["reason"] == "pg_lookup_exception"

    @pytest.mark.asyncio
    async def test_dispatch_retry_declares_dispatch_auto_mint(self):
        """T4: dispatch-time middleware retry on session_resolve_miss declares
        spawn_reason='dispatch_auto_mint' so the fresh identity carries
        lineage rather than swelling the no-lineage ghost rate.

        Without this declaration S21-a stops the Redis-overwrite bleed but
        leaves the lineage-declaration bleed open — the council
        (conceptual reviewer, 2026-04-27) flagged this as the test that
        would have caught the lineage gap.

        Strategy: source-level assertion. The middleware imports
        resolve_session_identity inside the function, so a runtime mock
        of the resolution path requires reproducing the entire dispatch
        entry. Instead we assert the contract directly: the retry
        invocation in identity_step.py mentions spawn_reason=
        "dispatch_auto_mint" alongside force_new=True. If a future
        refactor drops or renames the kwarg, this test catches it before
        the no-lineage bleed reopens.
        """
        from pathlib import Path
        src_path = (Path(__file__).parent.parent
                    / "src" / "mcp_handlers" / "middleware" / "identity_step.py")
        source = src_path.read_text()

        # The retry block must mint with force_new=True AND declare a
        # spawn_reason so the audit log has lineage. Both kwargs must
        # appear inside the same call near the session_resolve_miss check.
        retry_idx = source.find("session_resolve_miss")
        assert retry_idx != -1, "session_resolve_miss branch missing from middleware"
        retry_window = source[retry_idx:retry_idx + 1500]
        assert "force_new=True" in retry_window, (
            "Dispatch retry must use force_new=True; otherwise the second "
            "resolve hits the same fail-closed path and recurses."
        )
        assert 'spawn_reason="dispatch_auto_mint"' in retry_window, (
            "Dispatch retry must declare spawn_reason='dispatch_auto_mint' "
            "so middleware-minted identities carry lineage. Without this "
            "S21-a stops the Redis-overwrite bleed but the no-lineage "
            "ghost-fork rate stays high."
        )

    @pytest.mark.asyncio
    async def test_resume_false_default_still_mints_on_miss(self):
        """Sanity: resume=False (default) preserves mint-on-miss behavior.

        Callers like handlers.py:424 and the onboard force_new path rely on
        the default fall-through to PATH 3 minting. Only resume=True now
        fail-closes on session-row miss.
        """
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        db = _make_db_no_session()

        with patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=db), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=db):
            result = await resolve_session_identity(
                session_key="agent-no-row-default",
                model_type="claude-opus-4",
                # resume defaults to False; force_new defaults to False
            )

        assert result.get("created") is True
        assert result.get("agent_uuid")


# ---------------------------------------------------------------------------
# T5 — onboard session_resolve_miss must persist with non-NULL spawn_reason
# (S21-a-followup, 2026-04-27 post-deploy canary)
# ---------------------------------------------------------------------------

class TestS21AFollowupSpawnReason:

    @pytest.mark.asyncio
    async def test_onboard_session_resolve_miss_persists_spawn_reason(self):
        """T5: when onboard hits session_resolve_miss with no caller-provided
        spawn_reason, the captured-fresh persistence path must write
        core.agents with spawn_reason='auto_onboard_no_session'.

        S21-a's load-bearing fix (mint_guard) prevents Redis ghost
        ratification but did not move the no-lineage ghost-fork rate —
        post-deploy canary on 2026-04-27 found 0 'dispatch_auto_mint' rows
        in core.agents because the spawn_reason set on the resolve retry
        was never plumbed to the eventual ensure_agent_persisted call.
        Setting the OUTER `_spawn_reason` variable (consumed at
        handlers.py:~L1497) is the actual fix.
        """
        from types import SimpleNamespace
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        # DB mocks: no prior session, no existing identity, then identity
        # appears after upsert (mirrors the captured-fresh persistence flow).
        db = AsyncMock()
        db.init = AsyncMock()
        db.get_session = AsyncMock(return_value=None)
        db.find_agent_by_label = AsyncMock(return_value=None)
        db.get_agent_label = AsyncMock(return_value=None)
        db.update_session_activity = AsyncMock()
        db.create_session = AsyncMock()
        db.upsert_agent = AsyncMock()
        db.upsert_identity = AsyncMock()
        db.update_agent_fields = AsyncMock(return_value=True)
        db.get_agent_thread_info = AsyncMock(return_value=None)
        db.get_thread_nodes = AsyncMock(return_value=[])
        db.create_or_get_thread = AsyncMock()
        db.claim_thread_position = AsyncMock(return_value=0)
        # ensure_agent_persisted: first lookup is None (not yet persisted),
        # second lookup returns the upserted row.
        db.get_identity = AsyncMock(side_effect=[
            None,  # resolve_session_identity PG lookup
            None,  # ensure_agent_persisted check
            SimpleNamespace(identity_id=42, metadata={}),  # after upsert
        ])
        db.get_agent = AsyncMock(return_value=None)

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.bind = AsyncMock()

        async def _get_raw():
            r = AsyncMock()
            r.get = AsyncMock(return_value=None)
            r.setex = AsyncMock()
            r.expire = AsyncMock()
            return r

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
            # client_session_id is a proof signal so S13's force_new gate
            # does NOT fire. resume=True default → PATH 2 fail-closes →
            # session_resolve_miss branch → captured-fresh path.
            await handle_onboard_v2({"client_session_id": "agent-no-prior"})

        # Inspect the upsert_agent calls. With the fix, the spawn_reason
        # must be non-NULL — defaulted to "auto_onboard_no_session" when
        # the caller didn't declare one.
        assert db.upsert_agent.called, "upsert_agent never called — captured-fresh path didn't run"
        call_kwargs = db.upsert_agent.call_args.kwargs
        spawn_reason = call_kwargs.get("spawn_reason")
        assert spawn_reason == "auto_onboard_no_session", (
            f"expected spawn_reason='auto_onboard_no_session', got {spawn_reason!r}; "
            f"the S21-a-followup outer-variable fix did not land. Without it, "
            f"every session_resolve_miss-driven onboard mint persists with NULL "
            f"spawn_reason and the no-lineage ghost-fork rate doesn't move."
        )


class TestS21BSpawnReasonPlumbing:

    @pytest.mark.asyncio
    async def test_cache_session_records_spawn_reason_in_memory(self):
        """S21-b: session binding cache must preserve fork lineage."""
        from src.mcp_handlers.identity.persistence import _cache_session

        session_key = "agent-s21b-cache"
        agent_uuid = "33333333-3333-4333-8333-333333333333"
        in_memory = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.identity.shared._session_identities", in_memory):
            await _cache_session(
                session_key,
                agent_uuid,
                display_agent_id=agent_uuid,
                spawn_reason="dispatch_auto_mint",
                mint_guard=True,
            )

        assert in_memory[session_key]["bound_agent_id"] == agent_uuid
        assert in_memory[session_key]["spawn_reason"] == "dispatch_auto_mint"

    @pytest.mark.asyncio
    async def test_force_new_lazy_persist_uses_cached_spawn_reason(self):
        """S21-b: middleware PATH-3 mints must keep lineage when persisted."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity
        from src.mcp_handlers.identity.persistence import ensure_agent_persisted

        db = _make_db_no_session()
        in_memory = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.identity.shared._session_identities", in_memory), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=db):
            result = await resolve_session_identity(
                session_key="agent-s21b-force-new",
                force_new=True,
                spawn_reason="dispatch_auto_mint",
            )
            await ensure_agent_persisted(
                result["agent_uuid"],
                "agent-s21b-force-new",
            )

        assert result["created"] is True
        assert result["spawn_reason"] == "dispatch_auto_mint"
        assert result["identity_resolution_outcome"] == "minted_after_resume_miss"
        assert db.upsert_agent.call_args.kwargs["spawn_reason"] == "dispatch_auto_mint"
        assert db.upsert_identity.call_args.kwargs["spawn_reason"] == "dispatch_auto_mint"
        assert in_memory["agent-s21b-force-new"]["spawn_reason"] == "dispatch_auto_mint"


# ---------------------------------------------------------------------------
# #425 — STRICT_IDENTITY_REQUIRED env flag for staged rollout of typed-refusal
# ---------------------------------------------------------------------------

class TestStrictIdentityRequiredFlag:
    """When STRICT_IDENTITY_REQUIRED=true, the dispatch retry replaces the
    auto-mint else branch with a typed-refusal response. Default is off so
    rollout is staged: local → Lumen → dispatch → flip default. The refusal
    is a structured success-shape (not an MCP error) so callers can't catch
    it and retry into the auto-mint path."""

    def test_strict_branch_present_in_source(self):
        """Source-level guard: the typed-refusal block must coexist with
        the auto-mint block, gated by os.getenv. If a future refactor drops
        either branch, this test fails before the production behavior
        regresses."""
        from pathlib import Path
        src_path = (Path(__file__).parent.parent
                    / "src" / "mcp_handlers" / "middleware" / "identity_step.py")
        source = src_path.read_text()

        retry_idx = source.find("session_resolve_miss")
        assert retry_idx != -1
        retry_window = source[retry_idx:retry_idx + 3500]

        assert 'is_strict_identity_required' in retry_window, (
            "Strict-mode branch must call the shared is_strict_identity_required() "
            "helper so all auto-mint paths use one gate (#425)."
        )
        assert '"status": "identity_required"' in retry_window, (
            "Typed-refusal must carry status='identity_required' so callers "
            "can structurally distinguish it from a success or error."
        )
        assert '"hint":' in retry_window and 'onboard()' in retry_window, (
            "Typed-refusal must hint at the next action (onboard call)."
        )
        # Auto-mint branch must still exist for non-strict mode (default).
        assert 'spawn_reason="dispatch_auto_mint"' in retry_window, (
            "Default-off path must still fall through to auto-mint; "
            "removing this would break existing callers before rollout."
        )

    def test_helper_default_is_false(self):
        # Verify the shared helper itself defaults to False when env unset —
        # belt-and-suspenders against a future commit that flips the default
        # before residents are migrated.
        import os
        from src.mcp_handlers.identity_bootstrap import is_strict_identity_required

        prior = os.environ.pop("STRICT_IDENTITY_REQUIRED", None)
        try:
            assert is_strict_identity_required() is False
        finally:
            if prior is not None:
                os.environ["STRICT_IDENTITY_REQUIRED"] = prior

    def test_helper_truthy_values(self):
        import os
        from src.mcp_handlers.identity_bootstrap import is_strict_identity_required

        prior = os.environ.get("STRICT_IDENTITY_REQUIRED")
        try:
            for val in ("1", "true", "TRUE", "yes", "Yes", "on"):
                os.environ["STRICT_IDENTITY_REQUIRED"] = val
                assert is_strict_identity_required() is True, f"value {val!r} should be truthy"
            for val in ("0", "false", "no", "off", "", "garbage"):
                os.environ["STRICT_IDENTITY_REQUIRED"] = val
                assert is_strict_identity_required() is False, f"value {val!r} should be falsy"
        finally:
            if prior is None:
                os.environ.pop("STRICT_IDENTITY_REQUIRED", None)
            else:
                os.environ["STRICT_IDENTITY_REQUIRED"] = prior


class TestStrictModeMintPathClosure:
    """#425 Paths B/C/D/E: source-level guards that each auto-mint path
    checks the shared is_strict_identity_required() helper before minting.

    Source-level rather than runtime because the runtime test surface (mocking
    the full dispatch + PG + Redis stack for each path) is heavy and brittle,
    and the regression risk we want to catch is "someone removes the gate" —
    a substring assertion on the file catches that cleanly.

    Each test names the path letter from the #425 council scan so the
    failure message points at the source-of-truth issue.
    """

    @staticmethod
    def _read(rel_path: str) -> str:
        from pathlib import Path
        return (Path(__file__).parent.parent / rel_path).read_text()

    def test_path_b_onboard_auto_onboard_no_session_gated(self):
        # Path B: onboard()'s auto_onboard_no_session branch should refuse
        # in strict mode if neither parent_agent_id nor force_new=True is
        # passed. Caller must declare lineage intent.
        source = self._read("src/mcp_handlers/identity/handlers.py")
        idx = source.find("auto_onboard_no_session")
        assert idx != -1
        window = source[idx:idx + 3000]
        assert "is_strict_identity_required" in window, (
            "Path B (#425): the auto_onboard_no_session branch must check "
            "is_strict_identity_required() before minting. Without this gate, "
            "bare onboard() continues to violate the lineage-declaration ontology."
        )
        assert "lineage_declaration_required" in window, (
            "Path B's strict-mode refusal must use status='lineage_declaration_required' "
            "to distinguish from the generic identity_required (the caller HAS an identity "
            "intent, just hasn't declared lineage)."
        )
        assert "parent_agent_id" in window and "force_new" in window, (
            "Path B's hint must mention both parent_agent_id and force_new so the caller "
            "can pick the right resolution."
        )

    def test_path_c_process_agent_update_self_create_gated(self):
        # Path C: process_agent_update self-create at the get_or_create_agent
        # call should refuse in strict mode. There are multiple `if
        # ctx.is_new_agent:` blocks in phases.py; we want the one in
        # `prepare_unlocked_inputs` that calls get_or_create_agent (the
        # self-create branch), not the onboarding-guidance branch in
        # resolve_identity_and_guards.
        source = self._read("src/mcp_handlers/updates/phases.py")
        idx = source.find("get_or_create_agent")
        assert idx != -1
        # Pre-window: the gate must precede the get_or_create_agent call.
        pre_window = source[max(0, idx - 2500):idx]
        assert "is_strict_identity_required" in pre_window, (
            "Path C (#425): process_agent_update's self-create branch must check "
            "is_strict_identity_required() before calling get_or_create_agent."
        )
        assert "Path C" in pre_window, (
            "Path C's strict block must reference '#425 Path C' in a comment so future "
            "audits can map symptom back to issue."
        )

    def test_path_d_record_agent_state_recovery_gated(self):
        # Path D: record_agent_state recovery should refuse in strict mode
        # if the agent isn't in PG. Missing-row at this layer indicates an
        # upstream orphan; fail loud.
        source = self._read("src/mcp_handlers/updates/phases.py")
        idx = source.find("Agent {agent_id} not found, creating")
        assert idx != -1
        # Pre-window: gate must come BEFORE the create_agent call.
        pre_window = source[max(0, idx - 1500):idx + 500]
        assert "is_strict_identity_required" in pre_window, (
            "Path D (#425): record_agent_state's recovery branch must check "
            "is_strict_identity_required() before calling create_agent."
        )
        assert "Path D" in pre_window, "Path D strict block must reference '#425 Path D'."

    def test_path_e_stdio_heartbeat_gated(self):
        # Path E: stdio heartbeat should skip metadata creation for unknown
        # agent_id in strict mode (don't create metadata for unregistered).
        source = self._read("src/mcp_server_std.py")
        idx = source.find("def inject_lightweight_heartbeat")
        assert idx != -1
        window = source[idx:idx + 2500]
        assert "is_strict_identity_required" in window, (
            "Path E (#425): inject_lightweight_heartbeat must check "
            "is_strict_identity_required() before calling get_or_create_metadata "
            "for an unknown agent_id. This path runs OUTSIDE the middleware so "
            "the dispatch allowlist doesn't reach it."
        )
        assert "Path E" in window, "Path E strict block must reference '#425 Path E'."
