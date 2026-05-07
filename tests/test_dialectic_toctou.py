"""
Regression tests for the dialectic synthesis TOCTOU lock (NEW-1, council 2026-05-06).

Bug: handle_submit_synthesis loaded → mutated → wrote without a row-level
lock. Two concurrent submit_synthesis calls with agrees=True from the two
participants could both pass the SYNTHESIS-phase check on their own in-memory
copies, both call finalize_resolution, and the second pg_resolve_session
overwrite the first.

Fix: per-session asyncio.Lock acquired around the load-mutate-persist
critical region in handle_submit_synthesis.
"""

import pytest
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
from types import SimpleNamespace
from datetime import datetime, timezone

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import DialecticSession, DialecticPhase, DialecticMessage
from src.mcp_handlers.dialectic.session import (
    get_session_lock,
    _SESSION_LOCKS,
    ACTIVE_SESSIONS,
)


@pytest.fixture(autouse=True)
def _clear_module_state():
    _SESSION_LOCKS.clear()
    ACTIVE_SESSIONS.clear()
    yield
    _SESSION_LOCKS.clear()
    ACTIVE_SESSIONS.clear()


class TestSessionLockIdentity:
    @pytest.mark.asyncio
    async def test_same_session_id_returns_same_lock(self):
        a = await get_session_lock("sess-x")
        b = await get_session_lock("sess-x")
        assert a is b, "lock dict must reuse a single Lock per session_id"

    @pytest.mark.asyncio
    async def test_different_session_ids_return_different_locks(self):
        a = await get_session_lock("sess-x")
        b = await get_session_lock("sess-y")
        assert a is not b, "different session_ids must get distinct locks"

    @pytest.mark.asyncio
    async def test_concurrent_first_acquire_is_safe(self):
        """Two concurrent first-acquires for the same session must converge to one Lock."""
        results = await asyncio.gather(
            *(get_session_lock("sess-race") for _ in range(20))
        )
        first = results[0]
        assert all(r is first for r in results), "dict-of-locks lazy create must be atomic"


class TestSynthesisHandlerSerializesPhaseTransition:
    """Verifies the lock actually blocks the synthesis handler under contention."""

    DIALECTIC = "src.mcp_handlers.dialectic.handlers"

    def _make_synthesis_session(self):
        """SYNTHESIS-phase session with thesis + antithesis already in transcript."""
        session = DialecticSession(
            paused_agent_id="agent-paused",
            reviewer_agent_id="agent-reviewer",
            session_type="recovery",
        )
        session.phase = DialecticPhase.SYNTHESIS
        session.synthesis_round = 1
        session.transcript.append(DialecticMessage(
            phase="thesis",
            agent_id="agent-paused",
            timestamp=datetime.now(timezone.utc).isoformat(),
            root_cause="initial cause",
            proposed_conditions=["c1"],
            reasoning="initial",
        ))
        session.transcript.append(DialecticMessage(
            phase="antithesis",
            agent_id="agent-reviewer",
            timestamp=datetime.now(timezone.utc).isoformat(),
            reasoning="counter",
            concerns=["concern-1"],
        ))
        return session

    def _mock_server(self):
        server = MagicMock()
        server.agent_metadata = {
            "agent-paused": SimpleNamespace(
                status="paused", api_key="key-a", label="A",
                last_update=datetime.now().isoformat(), paused_at=None, structured_id=None,
            ),
            "agent-reviewer": SimpleNamespace(
                status="active", api_key="key-b", label="B",
                last_update=datetime.now().isoformat(), paused_at=None, structured_id=None,
            ),
        }
        server.load_metadata_async = AsyncMock(return_value=None)
        server.monitors = {}
        return server

    @pytest.mark.asyncio
    async def test_handler_blocks_when_session_lock_held_externally(self):
        """If something else holds the per-session lock, handle_submit_synthesis
        must not proceed past the lock acquire. This proves the wiring is
        actually around the critical region, not a no-op."""
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = self._make_synthesis_session()
        sid = session.session_id

        lock = await get_session_lock(sid)
        await lock.acquire()
        try:
            with patch(f"{self.DIALECTIC}.mcp_server", self._mock_server()), \
                 patch("src.mcp_handlers.shared.get_mcp_server", return_value=self._mock_server()), \
                 patch(f"{self.DIALECTIC}.load_session", new_callable=AsyncMock,
                       return_value=session), \
                 patch(f"{self.DIALECTIC}.pg_add_message", new_callable=AsyncMock), \
                 patch(f"{self.DIALECTIC}.pg_update_phase", new_callable=AsyncMock), \
                 patch(f"{self.DIALECTIC}.save_session", new_callable=AsyncMock), \
                 patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
                handler_task = asyncio.create_task(handle_submit_synthesis({
                    "session_id": sid,
                    "agent_id": "agent-paused",
                    "proposed_conditions": ["c1"],
                    "reasoning": "ok",
                    "agrees": False,
                    "api_key": "key-a",
                }))
                # Give the handler time to reach the lock acquire and block.
                # If the lock weren't held around the critical region, the
                # handler would race to completion before this sleep returns.
                await asyncio.sleep(0.2)
                assert not handler_task.done(), (
                    "handler must block on session_lock; if it completed, "
                    "the lock is not actually around the critical region"
                )
        finally:
            lock.release()

        # Now the handler should be able to proceed and return.
        result = await asyncio.wait_for(handler_task, timeout=5.0)
        # We don't assert success/failure shape — only that it ran to completion
        # once the contended lock was released.
        assert result is not None

    @pytest.mark.asyncio
    async def test_concurrent_synthesis_does_not_double_resolve(self):
        """Two concurrent submit_synthesis calls with agrees=True against the
        same SYNTHESIS-phase session: only one converges. The other arrives
        with the in-memory phase already RESOLVED and is rejected by
        DialecticSession.submit_synthesis's phase check.
        """
        from src.mcp_handlers.dialectic.handlers import handle_submit_synthesis

        session = self._make_synthesis_session()
        sid = session.session_id

        # Both handlers see the SAME in-memory session (this is the actual
        # production behavior under ACTIVE_SESSIONS reuse). Without the lock,
        # both load it concurrently in SYNTHESIS phase, both submit_synthesis
        # paths would race on phase mutation. With the lock, the second
        # handler enters serially and observes phase=RESOLVED.
        with patch(f"{self.DIALECTIC}.mcp_server", self._mock_server()), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=self._mock_server()), \
             patch(f"{self.DIALECTIC}.load_session", new_callable=AsyncMock,
                   return_value=session), \
             patch(f"{self.DIALECTIC}.pg_add_message", new_callable=AsyncMock), \
             patch(f"{self.DIALECTIC}.pg_update_phase", new_callable=AsyncMock), \
             patch(f"{self.DIALECTIC}.pg_resolve_session", new_callable=AsyncMock), \
             patch(f"{self.DIALECTIC}.save_session", new_callable=AsyncMock), \
             patch(f"{self.DIALECTIC}.execute_resolution", new_callable=AsyncMock,
                   return_value={"success": True, "agent_id": "agent-paused", "new_status": "active"}), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
            results = await asyncio.gather(
                handle_submit_synthesis({
                    "session_id": sid,
                    "agent_id": "agent-paused",
                    "proposed_conditions": ["agreed condition"],
                    "root_cause": "agreed cause",
                    "reasoning": "agree",
                    "agrees": True,
                    "api_key": "key-a",
                }),
                handle_submit_synthesis({
                    "session_id": sid,
                    "agent_id": "agent-reviewer",
                    "proposed_conditions": ["agreed condition"],
                    "root_cause": "agreed cause",
                    "reasoning": "agree",
                    "agrees": True,
                    "api_key": "key-b",
                }),
                return_exceptions=True,
            )

        assert session.phase == DialecticPhase.RESOLVED, (
            "exactly one synthesis path must transition session to RESOLVED"
        )
        # The RESOLVED-rejecting branch returns success=False with a phase
        # error. The converging branch returns success=True with converged=True.
        # Parse both result envelopes and assert exactly one converged.
        import json
        successes = []
        for r in results:
            if isinstance(r, Exception):
                continue
            payload = r[0].text if hasattr(r[0], "text") else str(r[0])
            try:
                parsed = json.loads(payload)
            except (ValueError, TypeError):
                continue
            if parsed.get("converged") is True:
                successes.append(parsed)

        assert len(successes) == 1, (
            f"exactly one synthesis must converge under contention, got "
            f"{len(successes)} successes; results={results}"
        )
