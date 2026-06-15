"""Tests for GovernanceAgent base class."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unitares_sdk.agent import CycleResult, GovernanceAgent
from unitares_sdk.client import GovernanceClient
from unitares_sdk.errors import IdentityDriftError, VerdictError
from unitares_sdk.models import CheckinResult, IdentityResult, OnboardResult


# --- CycleResult ---


class TestCycleResult:
    def test_simple_factory(self):
        r = CycleResult.simple("did some work")
        assert r.summary == "did some work"
        assert r.complexity == 0.3
        assert r.confidence == 0.7
        assert r.response_mode == "compact"
        assert r.notes is None

    def test_full_construction(self):
        r = CycleResult(
            summary="cleaned 3 entries",
            complexity=0.6,
            confidence=0.85,
            response_mode="full",
            notes=[("entry 1 cleaned", ["vigil", "cleanup"])],
        )
        assert r.complexity == 0.6
        assert len(r.notes) == 1
        assert r.notes[0][1] == ["vigil", "cleanup"]


# --- Test Agent Implementation ---


class SimpleAgent(GovernanceAgent):
    """Minimal test agent."""

    def __init__(self, cycle_result=None, **kwargs):
        super().__init__("TestAgent", **kwargs)
        self._cycle_result = cycle_result
        self.cycle_count = 0

    async def run_cycle(self, client: GovernanceClient) -> CycleResult | None:
        self.cycle_count += 1
        return self._cycle_result


# --- Helpers ---


def _make_token(aid: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"aid": aid}).encode()).decode().rstrip("=")
    return f"v1.{payload}.sig"


def _mock_client_connected():
    """Create a mocked GovernanceClient that behaves as if connected."""
    client = AsyncMock(spec=GovernanceClient)
    client.client_session_id = "sid-test"
    client.continuity_token = _make_token("uuid-test")
    client.agent_uuid = "uuid-test"

    client.identity = AsyncMock(return_value=IdentityResult(
        client_session_id="sid-test",
        uuid="uuid-test",
        continuity_token=_make_token("uuid-test"),
    ))
    client.onboard = AsyncMock(return_value=OnboardResult(
        success=True,
        client_session_id="sid-test",
        uuid="uuid-test",
    ))
    client.checkin = AsyncMock(return_value=CheckinResult(
        success=True,
        verdict="proceed",
    ))
    client.leave_note = AsyncMock()
    return client


def _call_tool_with_tags(existing_tags, *, fail_update=False):
    """An AsyncMock for client.call_tool that answers get_agent_metadata with
    ``existing_tags`` and records update_agent_metadata writes. ``fail_update``
    makes the write raise (to exercise the non-fatal path)."""

    async def _impl(tool, args, **kwargs):
        if tool == "get_agent_metadata":
            return {"success": True, "tags": list(existing_tags)}
        if tool == "update_agent_metadata":
            if fail_update:
                raise RuntimeError("write down")
            return {"success": True, "tags": args.get("tags")}
        return {"success": True}

    return AsyncMock(side_effect=_impl)


def _tool_writes(client):
    """The argument dicts passed to every update_agent_metadata call."""
    return [
        c.args[1]
        for c in client.call_tool.await_args_list
        if c.args and c.args[0] == "update_agent_metadata"
    ]


def _tool_reads(client):
    """The target_agent of every get_agent_metadata call."""
    return [
        c.args[1].get("target_agent")
        for c in client.call_tool.await_args_list
        if c.args and c.args[0] == "get_agent_metadata"
    ]


# --- Identity resolution ---


class TestIdentityResolution:
    @pytest.mark.asyncio
    async def test_uuid_resume_fast_path(self, tmp_path):
        """When agent_uuid is stored, should call identity(agent_uuid=...) directly."""
        agent = SimpleAgent(session_file=tmp_path / ".test_session")
        agent.agent_uuid = "uuid-test"

        client = _mock_client_connected()
        await agent._ensure_identity(client)

        client.identity.assert_called_once()
        args = client.identity.call_args
        assert args.kwargs.get("agent_uuid") == "uuid-test"
        assert args.kwargs.get("continuity_token") == _make_token("uuid-test")
        assert args.kwargs.get("resume") is True
        client.onboard.assert_not_called()

    @pytest.mark.asyncio
    async def test_uuid_lookup_failure_raises(self, tmp_path):
        """If UUID lookup fails, must raise — never silently create a ghost."""
        agent = SimpleAgent(session_file=tmp_path / ".test_session")
        agent.agent_uuid = "uuid-dead"

        client = _mock_client_connected()
        client.identity = AsyncMock(side_effect=Exception("uuid_not_found"))
        with pytest.raises(Exception, match="uuid_not_found"):
            await agent._ensure_identity(client)

        # Must NOT fall through to onboard
        client.onboard.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_onboard_when_no_uuid(self, tmp_path):
        """If no stored UUID, should onboard fresh."""
        agent = SimpleAgent(session_file=tmp_path / ".test_session")

        client = _mock_client_connected()
        await agent._ensure_identity(client)

        client.onboard.assert_called_once_with("TestAgent")
        client.identity.assert_not_called()

    @pytest.mark.asyncio
    async def test_onboard_forwards_parent_agent_id(self, tmp_path):
        """When configured, parent_agent_id + spawn_reason reach the onboard call."""
        agent = SimpleAgent(
            session_file=tmp_path / ".test_session",
            parent_agent_id="parent-uuid-123",
            spawn_reason="subagent",
        )

        client = _mock_client_connected()
        await agent._ensure_identity(client)

        client.onboard.assert_called_once_with(
            "TestAgent",
            parent_agent_id="parent-uuid-123",
            spawn_reason="subagent",
        )

    @pytest.mark.asyncio
    async def test_onboard_omits_lineage_when_unset(self, tmp_path):
        """Default (no parent) must preserve backward-compatible onboard call shape."""
        agent = SimpleAgent(session_file=tmp_path / ".test_session")

        client = _mock_client_connected()
        await agent._ensure_identity(client)

        # No parent_agent_id / spawn_reason kwargs when agent didn't set them
        call_kwargs = client.onboard.call_args.kwargs
        assert "parent_agent_id" not in call_kwargs
        assert "spawn_reason" not in call_kwargs

    @pytest.mark.asyncio
    async def test_resident_tags_stamped_on_fresh_onboard(self, tmp_path):
        """persistent=True agents stamp the full resident tag set after fresh onboard.

        Residents need BOTH 'persistent' (exempts orphan-sweep) AND 'autonomous'
        (exempts loop-detection pattern 4). Steward hit the pattern-4 gap on
        2026-04-20 because this path stamped only 'persistent'; once every 5min
        its sync was rejected, starving core.agent_state. RESIDENT_TAGS is the
        single source of truth.

        Against a pre-Phase-1 server (no server-side onboard stamp) the
        reconcile reads an empty tag set then writes RESIDENT_TAGS.
        """
        from unitares_sdk.agent import RESIDENT_TAGS
        assert "persistent" in RESIDENT_TAGS
        assert "autonomous" in RESIDENT_TAGS

        agent = SimpleAgent(session_file=tmp_path / ".test_session", persistent=True)

        client = _mock_client_connected()
        client.call_tool = _call_tool_with_tags([])  # server hasn't stamped yet
        await agent._ensure_identity(client)

        # Fresh onboard, then read-then-write reconcile.
        client.onboard.assert_called_once()
        writes = _tool_writes(client)
        assert writes == [{"agent_id": "uuid-test", "tags": RESIDENT_TAGS}]

    @pytest.mark.asyncio
    async def test_resident_tags_noop_on_fresh_onboard_when_server_stamped(self, tmp_path):
        """Against a current server the onboard handler already stamped the tags,
        so the SDK reconcile reads them present and issues no redundant write."""
        from unitares_sdk.agent import RESIDENT_TAGS

        agent = SimpleAgent(session_file=tmp_path / ".test_session", persistent=True)

        client = _mock_client_connected()
        client.call_tool = _call_tool_with_tags(list(RESIDENT_TAGS))
        await agent._ensure_identity(client)

        client.onboard.assert_called_once()
        assert _tool_writes(client) == []  # read-only, no update

    @pytest.mark.asyncio
    async def test_persistent_tag_not_stamped_when_not_persistent(self, tmp_path):
        """Default persistent=False must not touch metadata at all."""
        agent = SimpleAgent(session_file=tmp_path / ".test_session")

        client = _mock_client_connected()
        await agent._ensure_identity(client)

        client.onboard.assert_called_once()
        client.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_resident_tags_reconciled_on_uuid_resume_when_complete(self, tmp_path):
        """Resuming a fully-tagged identity reads the tags but writes nothing."""
        from unitares_sdk.agent import RESIDENT_TAGS

        agent = SimpleAgent(session_file=tmp_path / ".test_session", persistent=True)
        agent.agent_uuid = "uuid-test"  # triggers the resume fast path

        client = _mock_client_connected()
        client.call_tool = _call_tool_with_tags(list(RESIDENT_TAGS))
        await agent._ensure_identity(client)

        client.identity.assert_awaited_once()
        client.onboard.assert_not_called()
        # Reconcile read happened, but no missing tag → no write.
        assert _tool_reads(client) == ["uuid-test"]
        assert _tool_writes(client) == []

    @pytest.mark.asyncio
    async def test_resident_tags_reconciled_on_uuid_resume_when_missing(self, tmp_path):
        """Sentinel 2026-06-15 regression: a resident that always resumes via UUID
        with a dropped 'autonomous' tag must self-heal on resume, not stay flagged
        by Vigil forever. The fresh-onboard stamp never re-fires for these agents.
        """
        agent = SimpleAgent(session_file=tmp_path / ".test_session", persistent=True)
        agent.agent_uuid = "uuid-test"  # resume fast path

        client = _mock_client_connected()
        client.call_tool = _call_tool_with_tags(["persistent"])  # missing 'autonomous'
        await agent._ensure_identity(client)

        client.identity.assert_awaited_once()
        client.onboard.assert_not_called()
        # Additive write restores the full set.
        assert _tool_writes(client) == [
            {"agent_id": "uuid-test", "tags": ["persistent", "autonomous"]},
        ]

    @pytest.mark.asyncio
    async def test_resident_tag_reconcile_preserves_role_tags(self, tmp_path):
        """Reconcile is additive: role/cadence tags (cadence.10min) are preserved,
        the missing required tag is appended. A bare RESIDENT_TAGS write would
        clobber them because update_agent_metadata REPLACES the list.
        """
        agent = SimpleAgent(session_file=tmp_path / ".test_session", persistent=True)
        agent.agent_uuid = "uuid-test"

        client = _mock_client_connected()
        client.call_tool = _call_tool_with_tags(["persistent", "cadence.10min"])
        await agent._ensure_identity(client)

        assert _tool_writes(client) == [
            {"agent_id": "uuid-test", "tags": ["persistent", "cadence.10min", "autonomous"]},
        ]

    @pytest.mark.asyncio
    async def test_resident_tags_reconciled_once_per_process(self, tmp_path):
        """A successful reconcile is not re-run on the next cycle (no per-cycle
        get_agent_metadata read). _ensure_identity runs every cycle, so this
        guards against a recurring substrate-tax read.
        """
        agent = SimpleAgent(session_file=tmp_path / ".test_session", persistent=True)
        agent.agent_uuid = "uuid-test"

        client = _mock_client_connected()
        client.call_tool = _call_tool_with_tags(["persistent"])

        await agent._ensure_identity(client)  # cycle 1: read + write
        await agent._ensure_identity(client)  # cycle 2: must not re-check

        assert _tool_reads(client) == ["uuid-test"]  # exactly one read total
        assert len(_tool_writes(client)) == 1

    @pytest.mark.asyncio
    async def test_resident_tag_read_failure_is_non_fatal_and_retries(self, tmp_path):
        """If the tag read fails we must NOT blind-write (clobber risk), must not
        raise, and must leave the door open to retry on the next cycle."""
        agent = SimpleAgent(session_file=tmp_path / ".test_session", persistent=True)
        agent.agent_uuid = "uuid-test"

        client = _mock_client_connected()
        client.call_tool = AsyncMock(side_effect=RuntimeError("read down"))

        # Must not raise — exception caught and logged.
        await agent._ensure_identity(client)

        client.identity.assert_awaited_once()
        assert _tool_writes(client) == []  # never a blind write
        assert agent._resident_tags_reconciled is False  # will retry next cycle

    @pytest.mark.asyncio
    async def test_resident_tag_write_failure_is_non_fatal_and_retries(self, tmp_path):
        """If the reconcile write fails the agent still runs and the flag stays
        unset so the next cycle retries the write."""
        agent = SimpleAgent(session_file=tmp_path / ".test_session", persistent=True)
        agent.agent_uuid = "uuid-test"

        client = _mock_client_connected()
        client.call_tool = _call_tool_with_tags(["persistent"], fail_update=True)

        await agent._ensure_identity(client)

        assert agent._resident_tags_reconciled is False
        # Read succeeded, write was attempted (and failed) — both awaited.
        assert _tool_reads(client) == ["uuid-test"]
        assert _tool_writes(client) == [
            {"agent_id": "uuid-test", "tags": ["persistent", "autonomous"]},
        ]
        # Identity is still established despite the write failure.
        assert agent.agent_uuid == "uuid-test"


# --- Session persistence ---


class TestSessionPersistence:
    def test_save_and_load(self, tmp_path):
        agent = SimpleAgent(session_file=tmp_path / ".test_session")
        agent.client_session_id = "sid-1"
        agent.continuity_token = "tok-1"
        agent.agent_uuid = "uuid-1"
        agent._save_session()

        agent2 = SimpleAgent(session_file=tmp_path / ".test_session")
        agent2._load_session()
        assert agent2.client_session_id == "sid-1"
        assert agent2.continuity_token == "tok-1"
        assert agent2.agent_uuid == "uuid-1"

    def test_persistent_uds_agent_saves_uuid_only(self, tmp_path, monkeypatch):
        """Substrate residents using UDS must not leave bearer tokens on disk."""
        monkeypatch.setenv("UNITARES_UDS_SOCKET", "/tmp/governance.sock")
        anchor = tmp_path / ".test_session"
        agent = SimpleAgent(session_file=anchor, persistent=True)
        agent.client_session_id = "sid-1"
        agent.continuity_token = "tok-1"
        agent.agent_uuid = "uuid-1"

        agent._save_session()

        assert json.loads(anchor.read_text()) == {"agent_uuid": "uuid-1"}

    def test_load_missing_file(self, tmp_path):
        agent = SimpleAgent(session_file=tmp_path / ".nope")
        agent._load_session()
        assert agent.client_session_id is None

    def test_default_session_file_is_home_anchor(self, tmp_path, monkeypatch):
        """Without an explicit session_file, default is ~/.unitares/anchors/<name>.json."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from unitares_sdk.agent import GovernanceAgent

        # Use the real class directly since SimpleAgent may override defaults.
        class _BareAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return None

        agent = _BareAgent(name="TestAgent")
        expected = Path(tmp_path) / ".unitares" / "anchors" / "testagent.json"
        assert agent.session_file == expected

    def test_save_creates_anchor_parent_dir(self, tmp_path):
        """_save_session mkdirs the anchor parent automatically."""
        anchor = tmp_path / "deep" / "nested" / "anchors" / "x.json"
        agent = SimpleAgent(session_file=anchor)
        agent.agent_uuid = "u-1"
        agent._save_session()
        assert anchor.exists()

    def test_migrates_from_legacy_when_anchor_missing(self, tmp_path):
        """Legacy session file is migrated to the anchor on first load."""
        anchor = tmp_path / "anchor.json"
        legacy = tmp_path / "legacy.session"
        legacy.write_text('{"agent_uuid": "u-legacy", "continuity_token": "t-legacy"}')

        agent = SimpleAgent(session_file=anchor, legacy_session_file=legacy)
        agent._load_session()

        assert agent.agent_uuid == "u-legacy"
        assert agent.continuity_token == "t-legacy"
        assert anchor.exists(), "anchor should have been written by migration"

    def test_anchor_wins_over_legacy_when_both_exist(self, tmp_path):
        """If both anchor and legacy exist, the anchor is the source of truth."""
        anchor = tmp_path / "anchor.json"
        anchor.write_text('{"agent_uuid": "u-anchor"}')
        legacy = tmp_path / "legacy.session"
        legacy.write_text('{"agent_uuid": "u-legacy"}')

        agent = SimpleAgent(session_file=anchor, legacy_session_file=legacy)
        agent._load_session()
        assert agent.agent_uuid == "u-anchor"


# --- Check-in handling ---


class TestCheckinHandling:
    @pytest.mark.asyncio
    async def test_cycle_result_triggers_checkin(self, tmp_path):
        agent = SimpleAgent(
            cycle_result=CycleResult.simple("did work"),
            session_file=tmp_path / ".test_session",
        )
        client = _mock_client_connected()

        await agent._handle_cycle_result(client, CycleResult.simple("did work"))
        client.checkin.assert_called_once()
        args = client.checkin.call_args
        assert args.kwargs["response_text"] == "did work"

    @pytest.mark.asyncio
    async def test_none_skips_checkin(self, tmp_path):
        agent = SimpleAgent(session_file=tmp_path / ".test_session")
        client = _mock_client_connected()

        await agent._handle_cycle_result(client, None)
        client.checkin.assert_not_called()

    @pytest.mark.asyncio
    async def test_notes_posted(self, tmp_path):
        agent = SimpleAgent(session_file=tmp_path / ".test_session")
        client = _mock_client_connected()

        result = CycleResult(
            summary="work done",
            notes=[
                ("note 1", ["tag1"]),
                ("note 2", ["tag2", "tag3"]),
            ],
        )
        await agent._handle_cycle_result(client, result)
        assert client.leave_note.call_count == 2

    @pytest.mark.asyncio
    async def test_pause_verdict_raises(self, tmp_path):
        agent = SimpleAgent(session_file=tmp_path / ".test_session")
        client = _mock_client_connected()
        client.checkin = AsyncMock(return_value=CheckinResult(
            success=True,
            verdict="pause",
            guidance="Entropy too high",
        ))

        with pytest.raises(VerdictError) as exc_info:
            await agent._handle_cycle_result(client, CycleResult.simple("test"))
        assert exc_info.value.verdict == "pause"


# --- Heartbeat ---


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_sends_heartbeat(self, tmp_path):
        agent = SimpleAgent(session_file=tmp_path / ".test_session")
        agent._last_checkin_time = 0  # long ago
        client = _mock_client_connected()

        await agent._send_heartbeat(client)
        client.checkin.assert_called_once()
        args = client.checkin.call_args
        assert args.kwargs["response_text"] == "heartbeat"
        assert args.kwargs["complexity"] == 0.05


# --- State persistence ---


class TestStatePersistence:
    def test_save_and_load_state(self, tmp_path):
        agent = SimpleAgent(state_dir=tmp_path / "test_state")
        agent.save_state({"health": "ok", "cycles": 42})

        loaded = agent.load_state()
        assert loaded["health"] == "ok"
        assert loaded["cycles"] == 42

    def test_load_missing_state(self, tmp_path):
        agent = SimpleAgent(state_dir=tmp_path / "nonexistent")
        assert agent.load_state() == {}


# --- Graceful shutdown ---


class TestGracefulShutdown:
    def test_handle_signal(self):
        agent = SimpleAgent()
        assert agent.running is True
        agent._handle_signal(2)  # SIGINT
        assert agent.running is False


# --- sync_from_client ---


class TestSyncFromClient:
    def test_copies_identity(self):
        agent = SimpleAgent()
        client = MagicMock()
        client.client_session_id = "sid-new"
        client.continuity_token = "tok-new"
        client.agent_uuid = "uuid-new"

        agent._sync_from_client(client)
        assert agent.client_session_id == "sid-new"
        assert agent.continuity_token == "tok-new"
        assert agent.agent_uuid == "uuid-new"

    def test_raises_on_drift(self):
        agent = SimpleAgent()
        agent.agent_uuid = "uuid-original"

        client = MagicMock()
        client.client_session_id = "sid"
        client.continuity_token = "tok"
        client.agent_uuid = "uuid-different"

        with pytest.raises(IdentityDriftError):
            agent._sync_from_client(client)


class TestCycleTimeout:
    @pytest.mark.asyncio
    async def test_cycle_timeout_fires(self):
        """run_once raises TimeoutError if the cycle exceeds cycle_timeout_seconds."""

        class SlowAgent(GovernanceAgent):
            async def run_cycle(self, client):
                await asyncio.sleep(10.0)
                return CycleResult.simple("never reached")

        agent = SlowAgent(
            name="Slow",
            mcp_url="http://127.0.0.1:9999/mcp/",
            cycle_timeout_seconds=0.05,
        )
        # Bypass network: patch the client context manager and identity
        with patch("unitares_sdk.agent.GovernanceClient") as mock_cm:
            mock_client = AsyncMock()
            mock_cm.return_value.__aenter__.return_value = mock_client
            mock_cm.return_value.__aexit__.return_value = None
            with patch.object(SlowAgent, "_ensure_identity", AsyncMock()):
                with pytest.raises(asyncio.TimeoutError):
                    await agent.run_once()

    @pytest.mark.asyncio
    async def test_cycle_timeout_none_means_no_bound(self):
        """cycle_timeout_seconds=None disables the wrapper (default); run_once completes."""

        class QuickAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return None  # skip checkin path entirely

        agent = QuickAgent(name="Quick", mcp_url="http://127.0.0.1:9999/mcp/")
        assert agent.cycle_timeout_seconds is None

        with patch("unitares_sdk.agent.GovernanceClient") as mock_cm:
            mock_client = AsyncMock()
            mock_cm.return_value.__aenter__.return_value = mock_client
            mock_cm.return_value.__aexit__.return_value = None
            with patch.object(QuickAgent, "_ensure_identity", AsyncMock()):
                # Must complete without raising — no wait_for wrapping when None.
                await agent.run_once()

    @pytest.mark.asyncio
    async def test_connect_params_thread_to_client(self):
        """connect_timeout/connect_retries reach the per-cycle GovernanceClient."""

        class QuickAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return None

        agent = QuickAgent(
            name="Tuned",
            mcp_url="http://127.0.0.1:9999/mcp/",
            connect_timeout=5.0,
            connect_retries=2,
        )
        assert agent.connect_timeout == 5.0
        assert agent.connect_retries == 2

        with patch("unitares_sdk.agent.GovernanceClient") as mock_cm:
            mock_client = AsyncMock()
            mock_cm.return_value.__aenter__.return_value = mock_client
            mock_cm.return_value.__aexit__.return_value = None
            with patch.object(QuickAgent, "_ensure_identity", AsyncMock()):
                await agent.run_once()
            kwargs = mock_cm.call_args.kwargs
            assert kwargs["connect_timeout"] == 5.0
            assert kwargs["connect_retries"] == 2

    @pytest.mark.asyncio
    async def test_connect_params_default_none_passed_through(self):
        """Defaults are None so the client resolves its own env/defaults."""

        class QuickAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return None

        agent = QuickAgent(name="Default", mcp_url="http://127.0.0.1:9999/mcp/")
        assert agent.connect_timeout is None
        assert agent.connect_retries is None

        with patch("unitares_sdk.agent.GovernanceClient") as mock_cm:
            mock_client = AsyncMock()
            mock_cm.return_value.__aenter__.return_value = mock_client
            mock_cm.return_value.__aexit__.return_value = None
            with patch.object(QuickAgent, "_ensure_identity", AsyncMock()):
                await agent.run_once()
            kwargs = mock_cm.call_args.kwargs
            assert kwargs["connect_timeout"] is None
            assert kwargs["connect_retries"] is None

    @pytest.mark.asyncio
    async def test_run_forever_respects_cycle_timeout(self):
        """run_forever also bounds each iteration by cycle_timeout_seconds."""

        timed_out = {"fired": False}

        class SlowForeverAgent(GovernanceAgent):
            async def run_cycle(self, client):
                self.running = False  # exit loop after this iteration
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    timed_out["fired"] = True
                    raise

        agent = SlowForeverAgent(
            name="SlowForever",
            mcp_url="http://127.0.0.1:9999/mcp/",
            cycle_timeout_seconds=0.05,
        )
        with patch("unitares_sdk.agent.GovernanceClient") as mock_cm:
            mock_client = AsyncMock()
            mock_cm.return_value.__aenter__.return_value = mock_client
            mock_cm.return_value.__aexit__.return_value = None
            with patch.object(SlowForeverAgent, "_ensure_identity", AsyncMock()):
                with patch.object(SlowForeverAgent, "_install_signal_handlers"):
                    # notify() may be called during error logging — stub it out.
                    with patch("unitares_sdk.agent.notify"):
                        # run_forever catches the TimeoutError internally and
                        # sleeps "interval" seconds before retrying; set interval
                        # to 0 and running=False inside run_cycle to exit.
                        await agent.run_forever(interval=0)
        assert timed_out["fired"] is True


class TestLogFileTrim:
    @pytest.mark.asyncio
    async def test_log_file_trimmed_after_cycle(self, tmp_path):
        """Base class trims log_file to max_log_lines after each cycle."""
        log_path = tmp_path / "agent.log"
        log_path.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")

        class LoggingAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return None  # skip checkin

        agent = LoggingAgent(
            name="Logger",
            mcp_url="http://127.0.0.1:9999/mcp/",
            log_file=log_path,
            max_log_lines=10,
        )
        with patch("unitares_sdk.agent.GovernanceClient") as mock_cm:
            mock_client = AsyncMock()
            mock_cm.return_value.__aenter__.return_value = mock_client
            mock_cm.return_value.__aexit__.return_value = None
            with patch.object(LoggingAgent, "_ensure_identity", AsyncMock()):
                await agent.run_once()

        surviving = log_path.read_text().splitlines()
        assert len(surviving) == 10
        assert surviving[-1] == "line 99"

    @pytest.mark.asyncio
    async def test_log_file_none_is_noop(self, tmp_path):
        """log_file=None (default) does not error."""

        class QuietAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return None

        agent = QuietAgent(name="Quiet", mcp_url="http://127.0.0.1:9999/mcp/")
        assert agent.log_file is None
        with patch("unitares_sdk.agent.GovernanceClient") as mock_cm:
            mock_client = AsyncMock()
            mock_cm.return_value.__aenter__.return_value = mock_client
            mock_cm.return_value.__aexit__.return_value = None
            with patch.object(QuietAgent, "_ensure_identity", AsyncMock()):
                await agent.run_once()

    @pytest.mark.asyncio
    async def test_log_file_trimmed_even_on_cycle_timeout(self, tmp_path):
        """Log trim happens in finally, so it fires even when the cycle times out."""
        log_path = tmp_path / "agent.log"
        log_path.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")

        class SlowAgent(GovernanceAgent):
            async def run_cycle(self, client):
                await asyncio.sleep(10.0)
                return None

        agent = SlowAgent(
            name="Slow",
            mcp_url="http://127.0.0.1:9999/mcp/",
            log_file=log_path,
            max_log_lines=5,
            cycle_timeout_seconds=0.05,
        )
        with patch("unitares_sdk.agent.GovernanceClient") as mock_cm:
            mock_client = AsyncMock()
            mock_cm.return_value.__aenter__.return_value = mock_client
            mock_cm.return_value.__aexit__.return_value = None
            with patch.object(SlowAgent, "_ensure_identity", AsyncMock()):
                with pytest.raises(asyncio.TimeoutError):
                    await agent.run_once()
        # Trim must have fired despite the TimeoutError.
        assert len(log_path.read_text().splitlines()) == 5


class TestOnAfterCheckin:
    @pytest.mark.asyncio
    async def test_hook_called_with_checkin_result(self):
        """on_after_checkin runs after a successful checkin with the result."""
        captured: dict = {}

        class HookedAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("did work")

            async def on_after_checkin(self, client, checkin_result, cycle_result):
                captured["checkin_verdict"] = checkin_result.verdict
                captured["cycle_summary"] = cycle_result.summary

        agent = HookedAgent(name="Hooked", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(
            return_value=CheckinResult(
                success=True, verdict="proceed", coherence=0.9,
                guidance="", metrics={},
            )
        )
        await agent._handle_cycle_result(mock_client, CycleResult.simple("did work"))

        assert captured["checkin_verdict"] == "proceed"
        assert captured["cycle_summary"] == "did work"

    @pytest.mark.asyncio
    async def test_hook_not_called_when_result_is_none(self):
        """on_after_checkin is skipped when run_cycle returned None."""
        captured: dict = {"called": False}

        class HookedAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return None

            async def on_after_checkin(self, client, checkin_result, cycle_result):
                captured["called"] = True

        agent = HookedAgent(name="Hooked", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        await agent._handle_cycle_result(mock_client, None)
        assert captured["called"] is False

    @pytest.mark.asyncio
    async def test_hook_runs_before_verdict_error_on_pause(self):
        """Hook runs on pause verdict before VerdictError is raised (when on_verdict_pause default False)."""
        captured: dict = {"called": False, "verdict": None}

        class HookedAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")

            async def on_after_checkin(self, client, checkin_result, cycle_result):
                captured["called"] = True
                captured["verdict"] = checkin_result.verdict

        agent = HookedAgent(name="Hooked", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(
            return_value=CheckinResult(
                success=True, verdict="pause", coherence=0.5,
                guidance="slow down", metrics={},
            )
        )
        with pytest.raises(VerdictError):
            await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))
        assert captured["called"] is True
        assert captured["verdict"] == "pause"

    @pytest.mark.asyncio
    async def test_hook_exception_does_not_break_cycle(self):
        """If on_after_checkin raises, it's logged but VerdictError is still decided from checkin_result."""

        class BrokenHookAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")

            async def on_after_checkin(self, client, checkin_result, cycle_result):
                raise RuntimeError("hook exploded")

        agent = BrokenHookAgent(name="Broken", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(
            return_value=CheckinResult(
                success=True, verdict="proceed", coherence=0.9,
                guidance="", metrics={},
            )
        )
        # Must NOT raise: hook failure is swallowed, verdict is proceed so no VerdictError.
        await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))

    @pytest.mark.asyncio
    async def test_hook_cancelled_error_propagates(self):
        """CancelledError from on_after_checkin is NOT swallowed (inherits from BaseException)."""

        class CancelledHookAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")

            async def on_after_checkin(self, client, checkin_result, cycle_result):
                raise asyncio.CancelledError("cancelled mid-hook")

        agent = CancelledHookAgent(name="Cancelled", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(
            return_value=CheckinResult(
                success=True, verdict="proceed", coherence=0.9,
                guidance="", metrics={},
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))


class TestOnVerdictPause:
    @pytest.mark.asyncio
    async def test_hook_can_request_retry(self):
        """on_verdict_pause returning True triggers a single checkin retry."""
        attempts: list = []

        class RecoveringAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")

            async def on_verdict_pause(self, client, checkin_result, cycle_result):
                attempts.append("recovery called")
                await client.self_recovery(action="quick")
                return True  # request retry

        agent = RecoveringAgent(name="Recoverer", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        first = CheckinResult(
            success=True, verdict="pause", coherence=0.5,
            guidance="slow", metrics={},
        )
        second = CheckinResult(
            success=True, verdict="proceed", coherence=0.8,
            guidance="", metrics={},
        )
        mock_client.checkin = AsyncMock(side_effect=[first, second])
        mock_client.self_recovery = AsyncMock()

        # Should NOT raise: retry succeeded.
        await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))

        assert attempts == ["recovery called"]
        assert mock_client.checkin.await_count == 2
        assert mock_client.self_recovery.await_count == 1

    @pytest.mark.asyncio
    async def test_hook_returning_false_surfaces_pause(self):
        """on_verdict_pause returning False lets VerdictError propagate."""

        class PassiveAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")

            async def on_verdict_pause(self, client, checkin_result, cycle_result):
                return False

        agent = PassiveAgent(name="Passive", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(
            return_value=CheckinResult(
                success=True, verdict="pause", coherence=0.5,
                guidance="slow", metrics={},
            )
        )
        with pytest.raises(VerdictError):
            await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))

    @pytest.mark.asyncio
    async def test_on_after_checkin_receives_post_retry_result(self):
        """When on_verdict_pause triggers retry, on_after_checkin sees the RETRIED (second) checkin."""
        captured: dict = {}

        class RetryAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")

            async def on_verdict_pause(self, client, checkin_result, cycle_result):
                return True  # force retry

            async def on_after_checkin(self, client, checkin_result, cycle_result):
                captured["verdict"] = checkin_result.verdict
                captured["coherence"] = checkin_result.coherence

        agent = RetryAgent(name="Retry", mcp_url="http://127.0.0.1:9999/mcp/")
        first = CheckinResult(
            success=True, verdict="pause", coherence=0.3,
            guidance="", metrics={},
        )
        second = CheckinResult(
            success=True, verdict="proceed", coherence=0.9,
            guidance="", metrics={},
        )
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(side_effect=[first, second])

        await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))

        # on_after_checkin must see the post-retry result, not the pre-retry pause.
        assert captured["verdict"] == "proceed"
        assert captured["coherence"] == 0.9

    @pytest.mark.asyncio
    async def test_default_hook_returns_false_so_pause_surfaces(self):
        """Default on_verdict_pause returns False — no retry, original pause raises."""

        class DefaultAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")
            # does NOT override on_verdict_pause

        agent = DefaultAgent(name="Default", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(
            return_value=CheckinResult(
                success=True, verdict="pause", coherence=0.5,
                guidance="slow", metrics={},
            )
        )
        with pytest.raises(VerdictError):
            await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))
        assert mock_client.checkin.await_count == 1  # no retry

    @pytest.mark.asyncio
    async def test_hook_exception_does_not_break_cycle(self):
        """If on_verdict_pause raises, the failure is logged and the original pause surfaces."""

        class BrokenRecoveryAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")

            async def on_verdict_pause(self, client, checkin_result, cycle_result):
                raise RuntimeError("recovery exploded")

        agent = BrokenRecoveryAgent(name="Broken", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(
            return_value=CheckinResult(
                success=True, verdict="pause", coherence=0.5,
                guidance="", metrics={},
            )
        )
        # Hook raised, no retry, original pause surfaces as VerdictError.
        with pytest.raises(VerdictError):
            await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))
        assert mock_client.checkin.await_count == 1

    @pytest.mark.asyncio
    async def test_hook_cancelled_error_propagates(self):
        """CancelledError from on_verdict_pause is NOT swallowed."""

        class CancelledRecoveryAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")

            async def on_verdict_pause(self, client, checkin_result, cycle_result):
                raise asyncio.CancelledError("cancelled mid-recovery")

        agent = CancelledRecoveryAgent(name="Cancelled", mcp_url="http://127.0.0.1:9999/mcp/")
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(
            return_value=CheckinResult(
                success=True, verdict="pause", coherence=0.5,
                guidance="", metrics={},
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))

    @pytest.mark.asyncio
    async def test_retry_also_pauses_raises_verdict_error_no_second_retry(self):
        """When the retry checkin also returns pause, VerdictError is raised — no second retry."""
        hook_calls: list = []

        class StubbornRecoveryAgent(GovernanceAgent):
            async def run_cycle(self, client):
                return CycleResult.simple("work")

            async def on_verdict_pause(self, client, checkin_result, cycle_result):
                hook_calls.append("called")
                return True

        agent = StubbornRecoveryAgent(name="Stubborn", mcp_url="http://127.0.0.1:9999/mcp/")
        first = CheckinResult(
            success=True, verdict="pause", coherence=0.3,
            guidance="first pause", metrics={},
        )
        second = CheckinResult(
            success=True, verdict="pause", coherence=0.35,
            guidance="still paused", metrics={},
        )
        mock_client = AsyncMock()
        mock_client.checkin = AsyncMock(side_effect=[first, second])

        with pytest.raises(VerdictError) as exc_info:
            await agent._handle_cycle_result(mock_client, CycleResult.simple("work"))

        # Exactly one retry, then give up — hook called once.
        assert hook_calls == ["called"]
        assert mock_client.checkin.await_count == 2
        # VerdictError carries the SECOND (final) pause's guidance.
        assert exc_info.value.guidance == "still paused"


class TestStateFileOverride:
    def test_state_file_override_used_for_persistence(self, tmp_path):
        """load_state / save_state use state_file when provided."""
        custom = tmp_path / "my_state.json"

        class Agent(GovernanceAgent):
            async def run_cycle(self, client):
                return None

        agent = Agent(
            name="Custom", mcp_url="http://127.0.0.1:9999/mcp/",
            state_file=custom,
        )
        agent.save_state({"cycles": 42})
        assert custom.exists()

        agent2 = Agent(
            name="Custom", mcp_url="http://127.0.0.1:9999/mcp/",
            state_file=custom,
        )
        assert agent2.load_state() == {"cycles": 42}

    def test_state_file_default_is_state_dir_over_state_json(self, tmp_path):
        """Default state path unchanged: state_dir/state.json."""

        class Agent(GovernanceAgent):
            async def run_cycle(self, client):
                return None

        agent = Agent(
            name="Default", mcp_url="http://127.0.0.1:9999/mcp/",
            state_dir=tmp_path,
        )
        agent.save_state({"k": "v"})
        assert (tmp_path / "state.json").exists()

    def test_state_file_takes_precedence_over_state_dir(self, tmp_path):
        """When both state_dir and state_file are given, state_file wins."""
        custom = tmp_path / "nested" / "chosen.json"
        other_dir = tmp_path / "elsewhere"

        class Agent(GovernanceAgent):
            async def run_cycle(self, client):
                return None

        agent = Agent(
            name="Picky", mcp_url="http://127.0.0.1:9999/mcp/",
            state_dir=other_dir,
            state_file=custom,
        )
        agent.save_state({"marker": 1})
        assert custom.exists()
        assert not (other_dir / "state.json").exists()


class TestRunForeverBackoff:
    """run_forever treats the typed 503 as an expected condition and backs
    off with the server-suggested delay instead of hammering on interval."""

    @staticmethod
    def _run(agent, interval):
        """Drive run_forever for exactly one failing iteration, capturing
        the loop's sleep delay. fake_sleep flips running=False so the loop
        exits after the first backoff sleep."""
        from unitares_sdk.errors import GovernanceUnavailableError  # noqa: F401

        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)
            agent.running = False

        async def _go():
            with patch("unitares_sdk.agent.GovernanceClient") as mock_cm:
                mock_client = AsyncMock()
                mock_cm.return_value.__aenter__.return_value = mock_client
                mock_cm.return_value.__aexit__.return_value = None
                with patch.object(type(agent), "_ensure_identity", AsyncMock()):
                    with patch.object(type(agent), "_install_signal_handlers"):
                        with patch("unitares_sdk.agent.notify"):
                            with patch("unitares_sdk.agent.asyncio.sleep", fake_sleep):
                                await agent.run_forever(interval=interval)
        return _go, sleeps

    @pytest.mark.asyncio
    async def test_unavailable_honors_server_retry_after(self):
        from unitares_sdk.errors import GovernanceUnavailableError

        class UnavailableAgent(GovernanceAgent):
            async def run_cycle(self, client):
                raise GovernanceUnavailableError(
                    "503", retry_after_seconds=17.0
                )

        agent = UnavailableAgent(name="U", mcp_url="http://127.0.0.1:9999/mcp/")
        go, sleeps = self._run(agent, interval=1)
        await go()
        assert sleeps == [17.0]

    @pytest.mark.asyncio
    async def test_unavailable_never_shrinks_interval(self):
        """A server delay shorter than the cycle interval must not speed
        the loop up."""
        from unitares_sdk.errors import GovernanceUnavailableError

        class UnavailableAgent(GovernanceAgent):
            async def run_cycle(self, client):
                raise GovernanceUnavailableError(
                    "503", retry_after_seconds=0.5
                )

        agent = UnavailableAgent(name="U", mcp_url="http://127.0.0.1:9999/mcp/")
        go, sleeps = self._run(agent, interval=30)
        await go()
        assert sleeps == [30.0]

    @pytest.mark.asyncio
    async def test_connection_error_keeps_interval(self):
        from unitares_sdk.errors import GovernanceConnectionError

        class DownAgent(GovernanceAgent):
            async def run_cycle(self, client):
                raise GovernanceConnectionError("refused")

        agent = DownAgent(name="D", mcp_url="http://127.0.0.1:9999/mcp/")
        go, sleeps = self._run(agent, interval=5)
        await go()
        assert sleeps == [5.0]
