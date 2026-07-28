"""Pre-mint identity recovery — a rotated session key must not fork the agent.

The failure this covers: `session_key` stability depends on `client_session_id`
being threaded on every MCP call. When it is not, derivation falls to the
onboard pin (step 7) and then to the bare IP:UA fingerprint. Once the pin
expires (30 min TTL), the derived key rotates, PATH 1/2 miss, and dispatch
retried with `force_new=True, spawn_reason="dispatch_auto_mint"` — minting a
phantom UUID for an agent that never went anywhere. EISV trajectories restart,
KG entries from the same agent look like strangers', and (2026-07-01, outcome
524032fd) a `record_result` write landed on the phantom with `success: true`.

`recover_identity_before_mint` is the last stop before `uuid.uuid4()`: it
consults the two fingerprint-anchored records that survive a key rotation
(the live onboard pin's UUID, then the longer-lived identity anchor) and
resumes instead of minting.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.middleware import DispatchContext
from src.mcp_handlers.middleware.identity_step import (
    _transport_identity_cache,
    resolve_identity,
)
from src.mcp_handlers.identity.resolution import recover_identity_before_mint


AGENT_UUID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
FINGERPRINT = "10.0.0.7:d20c2f"
BASE_FP = "ua:d20c2f"
STABLE_CSID = "claude-code-session-42"


@dataclass
class FakeSignals:
    """Minimal SessionSignals stand-in.

    transport="mcp" with no mcp_session_id is the exact shape with no sticky
    transport-cache key (co-resident MCP processes share IP:UA), so the pin /
    anchor really is the only bridge — the gap this feature closes.
    """
    mcp_session_id: Optional[str] = None
    x_session_id: Optional[str] = None
    x_client_id: Optional[str] = None
    oauth_client_id: Optional[str] = None
    ip_ua_fingerprint: Optional[str] = FINGERPRINT
    user_agent: Optional[str] = "claude-code/1.0"
    client_hint: Optional[str] = None
    x_agent_name: Optional[str] = None
    x_agent_id: Optional[str] = None
    transport: str = "mcp"
    peer_pid: Optional[int] = None
    unitares_operator_token: Optional[str] = None


class FakeRedis:
    """In-memory raw-Redis stand-in with the handful of ops the paths use."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def expire(self, key, ttl):
        return key in self.store

    async def delete(self, key):
        self.store.pop(key, None)

    async def ttl(self, key):
        return 900 if key in self.store else -2


def _make_db(*, has_session=False, agent_status="active"):
    """DB double: no session row for the rotated key, but the agent is alive."""
    db = MagicMock()
    db.init = AsyncMock()
    db.get_session = AsyncMock(return_value=None if not has_session else MagicMock(agent_id=AGENT_UUID))
    db.update_session_activity = AsyncMock()
    db.create_session = AsyncMock()
    db.upsert_agent = AsyncMock()
    db.upsert_identity = AsyncMock()
    db.get_identity = AsyncMock(return_value=MagicMock(identity_id="ident-1"))
    db.get_agent = AsyncMock(return_value=MagicMock(agent_id=AGENT_UUID, status=agent_status))
    return db


def _patches(redis, db, *, agent_exists=True, agent_status="active"):
    """Common patch stack: raw Redis, DB, and the PG existence/status probes."""
    async def _get_raw():
        return redis

    return [
        patch("src.cache.redis_client.get_redis", new=_get_raw),
        patch("src.mcp_handlers.identity.persistence._redis_cache", False),
        patch("src.mcp_handlers.identity.resolution.get_db", return_value=db),
        patch("src.mcp_handlers.identity.persistence.get_db", return_value=db),
        patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            AsyncMock(return_value=agent_exists),
        ),
        patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            AsyncMock(return_value=agent_status),
        ),
        patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            AsyncMock(return_value="claude-code-opus_aaaaaaaa"),
        ),
        patch(
            "src.mcp_handlers.identity.resolution._get_agent_id_from_metadata",
            AsyncMock(return_value="Claude_Opus_5_20260728"),
        ),
        # Imported inside _substrate_http_reject, so patch it at its home
        # module: none of these fixtures are substrate-anchored residents.
        patch(
            "src.substrate.verification.fetch_substrate_claim",
            AsyncMock(return_value=None),
        ),
    ]


class _Stack:
    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


@pytest.fixture(autouse=True)
def _clean_transport_cache():
    _transport_identity_cache.clear()
    yield
    _transport_identity_cache.clear()


# ---------------------------------------------------------------------------
# recover_identity_before_mint — the ladder itself
# ---------------------------------------------------------------------------

class TestRecoveryLadder:

    @pytest.mark.asyncio
    async def test_recovers_from_live_onboard_pin(self):
        """Pin alive but its session row gone: resume the pinned UUID.

        Session TTL and pin TTL are independent, so a session row can expire
        under a still-live pin. The pinned UUID is a declared identity — it
        beats minting.
        """
        redis = FakeRedis({
            f"recent_onboard:{BASE_FP}": json.dumps({
                "agent_uuid": AGENT_UUID,
                "client_session_id": STABLE_CSID,
            }),
        })
        db = _make_db()
        with _Stack(_patches(redis, db)):
            result = await recover_identity_before_mint(
                FINGERPRINT, signals=FakeSignals(),
            )

        assert result is not None
        assert result["agent_uuid"] == AGENT_UUID
        assert result["created"] is False
        assert result["identity_resolution_outcome"] == "resumed"
        assert result["recovered_via"] == "onboard_pin"
        assert result["recovered_client_session_id"] == STABLE_CSID

    @pytest.mark.asyncio
    async def test_recovers_from_anchor_after_pin_expiry(self):
        """The headline case: pin expired, anchor survives, same UUID returned."""
        redis = FakeRedis({
            # No recent_onboard:* key — the pin has expired.
            f"identity_anchor:{BASE_FP}": json.dumps({
                "agent_uuid": AGENT_UUID,
                "session_key": STABLE_CSID,
                "client_session_id": STABLE_CSID,
            }),
        })
        db = _make_db()
        with _Stack(_patches(redis, db)):
            result = await recover_identity_before_mint(
                FINGERPRINT, signals=FakeSignals(),
            )

        assert result is not None
        assert result["agent_uuid"] == AGENT_UUID
        assert result["recovered_via"] == "identity_anchor"

    @pytest.mark.asyncio
    async def test_rebinds_session_so_next_call_resumes_normally(self):
        """Recovery must heal the binding, not just answer this one call."""
        redis = FakeRedis({
            f"identity_anchor:{BASE_FP}": json.dumps({
                "agent_uuid": AGENT_UUID, "client_session_id": STABLE_CSID,
            }),
        })
        db = _make_db()
        with _Stack(_patches(redis, db)):
            await recover_identity_before_mint(
                "rotated-key-9", signals=FakeSignals(),
            )

        assert db.create_session.await_count == 1
        assert db.create_session.await_args.kwargs["session_id"] == "rotated-key-9"

    @pytest.mark.asyncio
    async def test_returns_none_when_nothing_anchored(self):
        """No pin, no anchor: minting IS the honest answer."""
        db = _make_db()
        with _Stack(_patches(FakeRedis(), db)):
            assert await recover_identity_before_mint(
                FINGERPRINT, signals=FakeSignals(),
            ) is None

    @pytest.mark.asyncio
    async def test_refuses_archived_agent(self):
        """An anchored-but-archived UUID is not resumable — fall through."""
        redis = FakeRedis({
            f"identity_anchor:{BASE_FP}": json.dumps({"agent_uuid": AGENT_UUID}),
        })
        db = _make_db()
        with _Stack(_patches(redis, db, agent_status="archived")):
            assert await recover_identity_before_mint(
                FINGERPRINT, signals=FakeSignals(),
            ) is None

    @pytest.mark.asyncio
    async def test_refuses_uuid_absent_from_postgres(self):
        """A dangling anchor must not resurrect a UUID nobody can resolve."""
        redis = FakeRedis({
            f"identity_anchor:{BASE_FP}": json.dumps({"agent_uuid": AGENT_UUID}),
        })
        db = _make_db()
        with _Stack(_patches(redis, db, agent_exists=False)):
            assert await recover_identity_before_mint(
                FINGERPRINT, signals=FakeSignals(),
            ) is None

    @pytest.mark.asyncio
    async def test_no_recovery_without_fingerprint(self):
        """Nothing to key on — no fingerprint, no recovery."""
        db = _make_db()
        with _Stack(_patches(FakeRedis(), db)):
            assert await recover_identity_before_mint(
                "k", signals=FakeSignals(ip_ua_fingerprint=None),
            ) is None

    @pytest.mark.asyncio
    async def test_disabled_by_flag(self, monkeypatch):
        monkeypatch.setenv("UNITARES_IDENTITY_ANCHOR_RECOVERY", "0")
        redis = FakeRedis({
            f"identity_anchor:{BASE_FP}": json.dumps({"agent_uuid": AGENT_UUID}),
        })
        db = _make_db()
        with _Stack(_patches(redis, db)):
            assert await recover_identity_before_mint(
                FINGERPRINT, signals=FakeSignals(),
            ) is None


# ---------------------------------------------------------------------------
# End-to-end through the dispatch middleware
# ---------------------------------------------------------------------------

class TestDispatchPrefersRecoveryOverMint:

    @pytest.mark.asyncio
    async def test_pin_expiry_does_not_mint_a_new_uuid(self):
        """Full path: a tool call with no client_session_id, after pin expiry,
        binds to the SAME uuid instead of minting."""
        redis = FakeRedis({
            f"identity_anchor:{BASE_FP}": json.dumps({
                "agent_uuid": AGENT_UUID,
                "session_key": STABLE_CSID,
                "client_session_id": STABLE_CSID,
            }),
        })
        db = _make_db()
        ctx = DispatchContext()
        arguments = {}

        with _Stack(_patches(redis, db)), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=FakeSignals()), \
             patch("src.mcp_handlers.middleware.identity_step.get_session_signals",
                   return_value=FakeSignals(), create=True), \
             patch("src.mcp_handlers.identity.session.get_session_signals",
                   return_value=FakeSignals(), create=True):
            _, arguments, ctx = await resolve_identity("sync_state", arguments, ctx)

        assert ctx.bound_agent_id == AGENT_UUID, (
            "dispatch minted a phantom UUID instead of recovering the anchored one"
        )
        assert ctx.identity_result["created"] is False
        assert ctx.identity_result["recovered_via"] == "identity_anchor"

    @pytest.mark.asyncio
    async def test_recovery_rethreads_client_session_id(self):
        """Ask #3: a call that omitted client_session_id gets it back from the
        recovered identity, so the rest of the request carries the stable proof
        string instead of re-deriving from the fingerprint."""
        redis = FakeRedis({
            f"identity_anchor:{BASE_FP}": json.dumps({
                "agent_uuid": AGENT_UUID, "client_session_id": STABLE_CSID,
            }),
        })
        db = _make_db()
        ctx = DispatchContext()

        with _Stack(_patches(redis, db)), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=FakeSignals()):
            _, arguments, ctx = await resolve_identity("sync_state", {}, ctx)

        assert arguments["client_session_id"] == STABLE_CSID
        assert ctx.client_session_id == STABLE_CSID

    @pytest.mark.asyncio
    async def test_rethreaded_csid_is_marked_transport_injected(self):
        """Recovery is server inference. It must not be laundered into
        caller-asserted proof for the strict write gate."""
        from src.mcp_handlers.context import (
            get_csid_transport_injected,
            set_csid_transport_injected,
        )
        redis = FakeRedis({
            f"identity_anchor:{BASE_FP}": json.dumps({
                "agent_uuid": AGENT_UUID, "client_session_id": STABLE_CSID,
            }),
        })
        db = _make_db()
        set_csid_transport_injected(False)

        with _Stack(_patches(redis, db)), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=FakeSignals()):
            await resolve_identity("sync_state", {}, DispatchContext())

        assert get_csid_transport_injected() is True

    @pytest.mark.asyncio
    async def test_still_mints_when_truly_unrecoverable(self):
        """The guard narrows minting; it must not abolish it. With nothing
        anchored, dispatch still produces a working identity."""
        db = _make_db()
        ctx = DispatchContext()

        with _Stack(_patches(FakeRedis(), db)), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=FakeSignals()):
            _, _, ctx = await resolve_identity("sync_state", {}, ctx)

        assert ctx.bound_agent_id is not None
        assert ctx.bound_agent_id != AGENT_UUID
        assert ctx.identity_result["created"] is True
        assert ctx.identity_result["spawn_reason"] == "dispatch_auto_mint"


# ---------------------------------------------------------------------------
# The anchor write — what makes a LATER call recoverable
# ---------------------------------------------------------------------------

class TestAnchorWrite:

    @staticmethod
    def _run_inline():
        """Spy for create_tracked_task that runs the coroutine to completion.

        The anchor write is fire-and-forget (off the request path), so a test
        that only awaited `resolve_identity` would race the background task.
        """
        scheduled = []

        def _spy(coro, name=None):
            scheduled.append(name)
            import asyncio as _a
            return _a.ensure_future(coro)

        return scheduled, _spy

    @pytest.mark.asyncio
    async def test_resumed_call_anchors_the_identity(self):
        """A successful PATH 2 resume writes the anchor a later rotated-key
        call will recover from."""
        redis = FakeRedis()
        db = _make_db(has_session=True)
        scheduled, spy = self._run_inline()

        with _Stack(_patches(redis, db)), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=FakeSignals()), \
             patch("src.background_tasks.create_tracked_task", side_effect=spy):
            _, _, ctx = await resolve_identity("sync_state", {}, DispatchContext())
            # Let the scheduled fire-and-forget writes drain.
            for _ in range(4):
                await __import__("asyncio").sleep(0)

        assert ctx.bound_agent_id == AGENT_UUID
        assert "identity_anchor_write" in scheduled
        anchored = [k for k in redis.store if k.startswith("identity_anchor:")]
        assert anchored, "a resumed dispatch must leave a recoverable anchor"
        assert json.loads(redis.store[anchored[0]])["agent_uuid"] == AGENT_UUID

    @pytest.mark.asyncio
    async def test_minted_identity_is_never_anchored(self):
        """Invariant: anchoring a phantom would make it sticky — strictly worse
        than the bug this feature fixes."""
        redis = FakeRedis()
        db = _make_db()  # no session row -> resolve miss -> dispatch_auto_mint
        scheduled, spy = self._run_inline()

        with _Stack(_patches(redis, db)), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=FakeSignals()), \
             patch("src.background_tasks.create_tracked_task", side_effect=spy):
            _, _, ctx = await resolve_identity("sync_state", {}, DispatchContext())
            for _ in range(4):
                await __import__("asyncio").sleep(0)

        assert ctx.identity_result["created"] is True
        assert "identity_anchor_write" not in scheduled
        assert not [k for k in redis.store if k.startswith("identity_anchor:")]


# ---------------------------------------------------------------------------
# #1319 boundary — a REFUSED resume is not a MISSING one
# ---------------------------------------------------------------------------

class TestHijackGuardBoundaryPreserved:

    def test_recovery_is_gated_on_session_resolve_miss_only(self):
        """Source-level guard. `resume_rejected_hijack_guard` must never reach
        the recovery ladder: recovering there would hand the hijack guard's
        refusal a fingerprint-shaped bypass, re-opening #1319 from the other
        side. If a refactor widens this condition, fail here first."""
        source = Path(
            project_root / "src/mcp_handlers/middleware/identity_step.py"
        ).read_text()
        idx = source.index("recover_identity_before_mint")
        window = source[max(0, idx - 900):idx]
        assert 'identity_result.get("error") == "session_resolve_miss"' in window, (
            "pre-mint recovery must be gated on session_resolve_miss alone"
        )


# ---------------------------------------------------------------------------
# Mid-session mints must be visible (ask #4)
# ---------------------------------------------------------------------------

class TestMidSessionMintIsLoud:

    @pytest.mark.asyncio
    async def test_post_resume_miss_mint_logs_a_warning(self, caplog):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        db = _make_db()
        with _Stack(_patches(FakeRedis(), db)), \
             patch("src.mcp_handlers.identity.shared._session_identities", {}), \
             caplog.at_level("WARNING"):
            result = await resolve_session_identity(
                session_key="rotated-key-1",
                force_new=True,
                spawn_reason="dispatch_auto_mint",
            )

        assert result["created"] is True
        assert "[MID_SESSION_MINT]" in caplog.text
        assert result["agent_uuid"][:8] in caplog.text

    @pytest.mark.asyncio
    async def test_declared_onboard_mint_stays_quiet(self, caplog):
        """A plain force_new onboard is the caller's stated intent, not a
        surprise — it must not spam the warning channel."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        db = _make_db()
        with _Stack(_patches(FakeRedis(), db)), \
             patch("src.mcp_handlers.identity.shared._session_identities", {}), \
             caplog.at_level("WARNING"):
            await resolve_session_identity(
                session_key="fresh-onboard-1",
                force_new=True,
                spawn_reason="new_session",
            )

        assert "[MID_SESSION_MINT]" not in caplog.text
