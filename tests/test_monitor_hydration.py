"""Tests for the four monitor-persistence fixes:

1. DB-fallback hydration via hydrate_from_db_if_fresh
2. Atomic file write in _write_state_file
3. asyncio.wait_for timeout budget on save_monitor_state_async
4. meta-None guards in agent_loop_detection.process_update_authenticated_async

Background (issue #138): core.agent_state and the JSON state file are
independent persistence paths. When file writes drop (anyio executor
stall, crash mid-write), the monitor reloaded as fresh update_count=0
and the agent displayed as "uninitialized" forever, even though the DB
had full history.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent_monitor_state import (
    STATE_SAVE_TIMEOUT_SECONDS,
    _write_state_file,
    ensure_hydrated,
    hydrate_from_db_if_fresh,
    save_monitor_state_async,
)


# ─── Fix #1: DB-fallback hydration ────────────────────────────────────────

class _FakeStateRow:
    """Shape of AgentStateRecord returned by db.get_agent_state_history."""

    def __init__(self, *, E, I, S, V, coherence, regime, state_json=None):
        self.energy = E
        self.integrity = I
        self.entropy = S
        self.void = V
        self.coherence = coherence
        self.regime = regime
        self.state_json = state_json or {}


def _make_fresh_monitor(agent_id="test-agent"):
    """Build a real UNITARESMonitor with a fresh state (update_count=0)."""
    from src.governance_monitor import UNITARESMonitor

    return UNITARESMonitor(agent_id, load_state=False)


@pytest.mark.asyncio
async def test_hydrate_from_db_if_fresh_populates_eisv_and_history():
    monitor = _make_fresh_monitor("hydrate-happy")
    assert monitor.state.update_count == 0  # precondition

    fake_identity = MagicMock(identity_id=42)
    fake_rows_desc = [
        # Most-recent-first (DB returns DESC)
        _FakeStateRow(E=0.7, I=0.8, S=0.1, V=-0.05, coherence=0.55, regime="DIVERGENCE"),
        _FakeStateRow(E=0.65, I=0.78, S=0.12, V=-0.03, coherence=0.52, regime="CONVERGENCE"),
        _FakeStateRow(E=0.6, I=0.75, S=0.15, V=0.0, coherence=0.5, regime="EXPLORATION"),
    ]
    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(return_value=fake_identity)
    fake_db.get_agent_state_history = AsyncMock(return_value=fake_rows_desc)

    with patch("src.db.get_db", return_value=fake_db):
        applied = await hydrate_from_db_if_fresh(monitor, "hydrate-happy")

    assert applied is True
    # update_count pulled from history length (gates the "uninitialized" display)
    assert monitor.state.update_count == 3
    # Latest EISV from the most-recent row (index 0 of the DESC list)
    assert monitor.state.unitaires_state.E == pytest.approx(0.7)
    assert monitor.state.unitaires_state.I == pytest.approx(0.8)
    assert monitor.state.unitaires_state.S == pytest.approx(0.1)
    assert monitor.state.unitaires_state.V == pytest.approx(-0.05)
    assert monitor.state.coherence == pytest.approx(0.55)
    assert monitor.state.regime == "DIVERGENCE"
    # Histories are chronological (oldest → newest)
    assert monitor.state.coherence_history == pytest.approx([0.5, 0.52, 0.55])
    assert monitor.state.regime_history == ["EXPLORATION", "CONVERGENCE", "DIVERGENCE"]


@pytest.mark.asyncio
async def test_hydrate_from_db_if_fresh_noop_when_monitor_already_has_updates():
    monitor = _make_fresh_monitor("hydrate-noop")
    monitor.state.update_count = 5  # already hydrated

    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(side_effect=AssertionError("should not be called"))

    with patch("src.db.get_db", return_value=fake_db):
        applied = await hydrate_from_db_if_fresh(monitor, "hydrate-noop")

    assert applied is False
    fake_db.get_identity.assert_not_called()
    assert monitor.state.update_count == 5


@pytest.mark.asyncio
async def test_hydrate_from_db_if_fresh_returns_false_when_no_identity():
    monitor = _make_fresh_monitor("hydrate-no-identity")
    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(return_value=None)

    with patch("src.db.get_db", return_value=fake_db):
        applied = await hydrate_from_db_if_fresh(monitor, "hydrate-no-identity")

    assert applied is False
    assert monitor.state.update_count == 0


@pytest.mark.asyncio
async def test_hydrate_rebuilds_decision_history_from_state_json_action():
    """state_json.action on each row → decision_history (chronological)."""
    monitor = _make_fresh_monitor("hydrate-actions")
    fake_identity = MagicMock(identity_id=7)
    # Most-recent-first per DB DESC ordering.
    fake_rows_desc = [
        _FakeStateRow(
            E=0.7, I=0.8, S=0.1, V=-0.05, coherence=0.55, regime="DIVERGENCE",
            state_json={"action": "proceed", "verdict": "safe"},
        ),
        _FakeStateRow(
            E=0.65, I=0.78, S=0.12, V=-0.03, coherence=0.52, regime="CONVERGENCE",
            state_json={"action": "reflect", "verdict": "caution"},
        ),
        _FakeStateRow(
            E=0.6, I=0.75, S=0.15, V=0.0, coherence=0.5, regime="EXPLORATION",
            state_json={"action": "proceed", "verdict": "safe"},
        ),
    ]
    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(return_value=fake_identity)
    fake_db.get_agent_state_history = AsyncMock(return_value=fake_rows_desc)

    with patch("src.db.get_db", return_value=fake_db):
        applied = await hydrate_from_db_if_fresh(monitor, "hydrate-actions")

    assert applied is True
    # Chronological (oldest → newest)
    assert monitor.state.decision_history == ["proceed", "reflect", "proceed"]
    # verdict_history hydrated from the same rows
    assert monitor.state.verdict_history == ["safe", "caution", "safe"]


@pytest.mark.asyncio
async def test_hydrate_skips_legacy_rows_missing_action_key():
    """Pre-action-write rows leave decision_history empty rather than crashing
    or polluting with verdict-vocabulary strings (the {safe,caution,high-risk}
    domain that pattern_analysis.py:204 doesn't bucket)."""
    monitor = _make_fresh_monitor("hydrate-legacy")
    fake_identity = MagicMock(identity_id=8)
    fake_rows_desc = [
        # Legacy rows: only verdict, no action — must not be promoted to actions.
        _FakeStateRow(
            E=0.7, I=0.8, S=0.1, V=-0.05, coherence=0.55, regime="DIVERGENCE",
            state_json={"verdict": "safe"},
        ),
        _FakeStateRow(
            E=0.65, I=0.78, S=0.12, V=-0.03, coherence=0.52, regime="CONVERGENCE",
            state_json={"verdict": "caution"},
        ),
    ]
    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(return_value=fake_identity)
    fake_db.get_agent_state_history = AsyncMock(return_value=fake_rows_desc)

    with patch("src.db.get_db", return_value=fake_db):
        applied = await hydrate_from_db_if_fresh(monitor, "hydrate-legacy")

    assert applied is True
    assert monitor.state.decision_history == []
    # verdict_history DOES populate from legacy rows — the verdict key
    # has been persisted since long before the action-write change, so
    # observe summary still surfaces a non-empty verdict_distribution.
    assert monitor.state.verdict_history == ["caution", "safe"]
    # Histories that should populate still do
    assert monitor.state.regime_history == ["CONVERGENCE", "DIVERGENCE"]


@pytest.mark.asyncio
async def test_hydrate_partial_action_coverage_keeps_only_present_rows():
    """Mixed legacy + new rows: only the rows with action are replayed."""
    monitor = _make_fresh_monitor("hydrate-partial")
    fake_identity = MagicMock(identity_id=9)
    fake_rows_desc = [
        _FakeStateRow(
            E=0.7, I=0.8, S=0.1, V=-0.05, coherence=0.55, regime="DIVERGENCE",
            state_json={"action": "pause", "verdict": "high-risk"},
        ),
        _FakeStateRow(  # legacy
            E=0.65, I=0.78, S=0.12, V=-0.03, coherence=0.52, regime="CONVERGENCE",
            state_json={"verdict": "caution"},
        ),
        _FakeStateRow(
            E=0.6, I=0.75, S=0.15, V=0.0, coherence=0.5, regime="EXPLORATION",
            state_json={"action": "proceed"},
        ),
    ]
    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(return_value=fake_identity)
    fake_db.get_agent_state_history = AsyncMock(return_value=fake_rows_desc)

    with patch("src.db.get_db", return_value=fake_db):
        applied = await hydrate_from_db_if_fresh(monitor, "hydrate-partial")

    assert applied is True
    # Chronological — legacy row in the middle is skipped.
    assert monitor.state.decision_history == ["proceed", "pause"]


@pytest.mark.asyncio
async def test_hydrate_from_db_if_fresh_returns_false_when_no_history():
    monitor = _make_fresh_monitor("hydrate-no-history")
    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(return_value=MagicMock(identity_id=1))
    fake_db.get_agent_state_history = AsyncMock(return_value=[])

    with patch("src.db.get_db", return_value=fake_db):
        applied = await hydrate_from_db_if_fresh(monitor, "hydrate-no-history")

    assert applied is False
    assert monitor.state.update_count == 0


@pytest.mark.asyncio
async def test_hydrate_from_db_if_fresh_swallows_db_exceptions():
    """Hydration must never raise — it's called unconditionally on every
    monitor access in async entry points."""
    monitor = _make_fresh_monitor("hydrate-explodes")
    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(side_effect=RuntimeError("DB exploded"))

    with patch("src.db.get_db", return_value=fake_db):
        applied = await hydrate_from_db_if_fresh(monitor, "hydrate-explodes")

    assert applied is False
    assert monitor.state.update_count == 0


# ─── Fix #5: get_or_create_monitor flag-and-drain wire-in ────────────────
#
# Cold factory + missing file + DB has rows must heal on the next async read.
# The factory marks `_needs_hydration=True` (sync) and async handlers drain
# the mark via `ensure_hydrated()`. Without these tests we have no regression
# guard against a future handler being added that skips the drain.


@pytest.mark.asyncio
async def test_factory_marks_needs_hydration_when_file_missing_and_meta_has_activity(tmp_path, monkeypatch):
    """Mnemos repro: file missing, meta says total_updates=9 → flag set."""
    from src.agent_lifecycle import get_or_create_monitor
    import src.agent_monitor_state as ams
    from src.agent_monitor_state import monitors

    agent_id = "factory-flag-set"
    monitors.pop(agent_id, None)
    monkeypatch.setattr(ams, "get_state_file", lambda _aid: tmp_path / "missing.json")

    fake_meta = MagicMock(total_updates=9)
    with patch("src.agent_lifecycle.get_or_create_metadata", return_value=fake_meta):
        monitor = get_or_create_monitor(agent_id)

    assert monitor._needs_hydration is True
    monitors.pop(agent_id, None)


@pytest.mark.asyncio
async def test_factory_does_not_mark_needs_hydration_for_genuinely_new_agent(tmp_path, monkeypatch):
    """Fresh-onboard agent with no prior activity must NOT trigger DB roundtrip.

    Saves DB cost on cold first-observe of every newly-onboarded agent and
    preserves the bootstrap-only "no measured trajectory" guard semantics.
    """
    from src.agent_lifecycle import get_or_create_monitor
    import src.agent_monitor_state as ams
    from src.agent_monitor_state import monitors

    agent_id = "factory-flag-unset"
    monitors.pop(agent_id, None)
    monkeypatch.setattr(ams, "get_state_file", lambda _aid: tmp_path / "missing.json")

    fake_meta = MagicMock(total_updates=0)
    with patch("src.agent_lifecycle.get_or_create_metadata", return_value=fake_meta):
        monitor = get_or_create_monitor(agent_id)

    assert monitor._needs_hydration is False
    monitors.pop(agent_id, None)


@pytest.mark.asyncio
async def test_factory_clears_needs_hydration_when_file_load_succeeds(tmp_path, monkeypatch):
    """File present → no need for DB hydration; flag must be False."""
    from src.agent_lifecycle import get_or_create_monitor
    import src.agent_monitor_state as ams
    from src.agent_monitor_state import monitors
    from src.governance_state import GovernanceState

    agent_id = "factory-file-loaded"
    monitors.pop(agent_id, None)

    fake_state = MagicMock(spec=GovernanceState, V_history=[1, 2, 3])
    monkeypatch.setattr(ams, "get_state_file", lambda _aid: tmp_path / "any.json")

    fake_meta = MagicMock(total_updates=9)
    with patch("src.agent_lifecycle.get_or_create_metadata", return_value=fake_meta), \
         patch("src.agent_lifecycle.load_monitor_state", return_value=fake_state):
        monitor = get_or_create_monitor(agent_id)

    assert monitor._needs_hydration is False
    monitors.pop(agent_id, None)


@pytest.mark.asyncio
async def test_ensure_hydrated_drains_flag_and_calls_hydrate():
    """Flag set → ensure_hydrated calls hydrate_from_db_if_fresh and clears flag."""
    monitor = _make_fresh_monitor("ensure-drain")
    monitor._needs_hydration = True

    fake_identity = MagicMock(identity_id=7)
    fake_rows = [_FakeStateRow(E=0.6, I=0.7, S=0.2, V=0.0, coherence=0.48, regime="EXPLORATION")]
    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(return_value=fake_identity)
    fake_db.get_agent_state_history = AsyncMock(return_value=fake_rows)

    with patch("src.db.get_db", return_value=fake_db):
        applied = await ensure_hydrated(monitor, "ensure-drain")

    assert applied is True
    assert monitor._needs_hydration is False
    assert monitor.state.update_count == 1
    assert monitor.state.coherence == pytest.approx(0.48)


@pytest.mark.asyncio
async def test_ensure_hydrated_noop_when_flag_unset():
    """Hot monitors (flag unset) must not trigger any DB call."""
    monitor = _make_fresh_monitor("ensure-noop")
    monitor._needs_hydration = False

    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(side_effect=AssertionError("must not be called"))

    with patch("src.db.get_db", return_value=fake_db):
        applied = await ensure_hydrated(monitor, "ensure-noop")

    assert applied is False
    fake_db.get_identity.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_hydrated_clears_flag_on_db_failure():
    """Single-shot semantics: DB unreachable still drains the flag — degrades
    to seed defaults on subsequent reads instead of retrying every time."""
    monitor = _make_fresh_monitor("ensure-db-down")
    monitor._needs_hydration = True

    fake_db = MagicMock()
    fake_db.get_identity = AsyncMock(side_effect=RuntimeError("DB down"))

    with patch("src.db.get_db", return_value=fake_db):
        applied = await ensure_hydrated(monitor, "ensure-db-down")

    assert applied is False
    assert monitor._needs_hydration is False  # drained even on failure
    assert monitor.state.update_count == 0  # seed defaults preserved


# ─── Fix #2: Atomic file write ────────────────────────────────────────────

def test_write_state_file_is_atomic_against_crash_mid_write(tmp_path):
    """A crash during json.dump must not leave a truncated / zero-byte file.

    We prove this by pre-populating the target path with known-good content,
    then making json.dump raise. The target file must still contain the old
    content afterwards — this is only possible with tempfile+os.replace.
    """
    state_file = tmp_path / "agent_state.json"
    state_file.write_text('{"old": "content"}')

    with patch("src.agent_monitor_state.json.dump", side_effect=RuntimeError("crash")):
        with pytest.raises(RuntimeError):
            _write_state_file(state_file, {"new": "data"})

    # Old content preserved — atomic rename never happened
    assert json.loads(state_file.read_text()) == {"old": "content"}
    # No leftover tempfiles
    leftover = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftover == [], f"tempfile not cleaned up: {leftover}"


def test_write_state_file_writes_valid_json_on_success(tmp_path):
    state_file = tmp_path / "agent_state.json"
    payload = {"E": 0.7, "I": 0.8, "update_count": 4}

    _write_state_file(state_file, payload)

    assert json.loads(state_file.read_text()) == payload


def test_write_state_file_creates_parent_dir(tmp_path):
    state_file = tmp_path / "nested" / "deeper" / "agent_state.json"
    _write_state_file(state_file, {"k": "v"})
    assert state_file.exists()


# ─── Fix #3: wait_for timeout on save ─────────────────────────────────────

@pytest.mark.asyncio
async def test_save_monitor_state_async_times_out_instead_of_hanging(tmp_path, monkeypatch):
    """If the run_in_executor await stalls (anyio executor starvation, etc.),
    save_monitor_state_async must drop the write rather than hang the handler."""
    # Redirect state file dir to a clean tmp location
    from src import agent_monitor_state

    def fake_get_state_file(agent_id):
        return tmp_path / f"{agent_id}_state.json"

    monkeypatch.setattr(agent_monitor_state, "get_state_file", fake_get_state_file)
    # Make the executor work hang longer than the timeout
    monkeypatch.setattr(
        agent_monitor_state,
        "STATE_SAVE_TIMEOUT_SECONDS",
        0.1,
    )

    monitor = _make_fresh_monitor("save-timeout")

    def _slow_write(*args, **kwargs):
        import time as _t
        _t.sleep(2.0)

    monkeypatch.setattr(agent_monitor_state, "_write_state_file", _slow_write)

    # Should return cleanly after the timeout, not hang for 2s
    start = asyncio.get_event_loop().time()
    await asyncio.wait_for(
        save_monitor_state_async("save-timeout", monitor),
        timeout=1.0,  # outer guard — if we hit this, the fix is broken
    )
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 1.0, f"save_monitor_state_async did not honor inner timeout: {elapsed}s"


@pytest.mark.asyncio
async def test_save_monitor_state_async_writes_normally_under_budget(tmp_path, monkeypatch):
    """Sanity: the happy path still produces a valid JSON file."""
    from src import agent_monitor_state

    def fake_get_state_file(agent_id):
        return tmp_path / f"{agent_id}_state.json"

    monkeypatch.setattr(agent_monitor_state, "get_state_file", fake_get_state_file)

    monitor = _make_fresh_monitor("save-happy")
    await save_monitor_state_async("save-happy", monitor)

    state_file = tmp_path / "save-happy_state.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    # Smoke check on the canonical fields
    assert "update_count" in data or "E" in data


# ─── Behavioral EISV survives restart (save was dropping it) ─────────────

def _advance_behavioral(monitor, n=4):
    """Advance the monitor's behavioral EMA so update_count > fallback threshold."""
    for _ in range(n):
        monitor._behavioral_state.update(0.6, 0.7, 0.25)
    return monitor._behavioral_state.update_count


def test_attach_behavioral_state_includes_ema():
    from types import SimpleNamespace
    from src.agent_monitor_state import _attach_behavioral_state
    from src.behavioral_state import BehavioralEISV

    beh = BehavioralEISV()
    for _ in range(3):
        beh.update(0.6, 0.7, 0.25)
    monitor = SimpleNamespace(_behavioral_state=beh)
    state_data = {}
    _attach_behavioral_state(monitor, state_data)

    assert "behavioral_eisv" in state_data
    assert state_data["behavioral_eisv"]["updates"] == 3


def test_attach_behavioral_state_failopen_when_absent():
    from types import SimpleNamespace
    from src.agent_monitor_state import _attach_behavioral_state

    # No _behavioral_state attribute → no key, no raise
    state_data = {}
    _attach_behavioral_state(SimpleNamespace(), state_data)
    assert "behavioral_eisv" not in state_data

    # _behavioral_state present but serialization raises → fail-open, no key
    class _Boom:
        def to_dict_with_history(self):
            raise RuntimeError("boom")

    state_data = {}
    _attach_behavioral_state(SimpleNamespace(_behavioral_state=_Boom()), state_data)
    assert "behavioral_eisv" not in state_data


@pytest.mark.asyncio
async def test_save_persists_behavioral_eisv_and_round_trips(tmp_path, monkeypatch):
    """Regression: the live save path dropped behavioral_eisv, so behavioral
    confidence reset to ODE-fallback on every restart. Save must now persist it,
    and the saved block must restore faithfully via from_dict (the load side)."""
    from src import agent_monitor_state
    from src.behavioral_state import BehavioralEISV

    monkeypatch.setattr(
        agent_monitor_state, "get_state_file",
        lambda agent_id: tmp_path / f"{agent_id}_state.json",
    )

    monitor = _make_fresh_monitor("beh-persist")
    count = _advance_behavioral(monitor, n=4)
    assert count == 4  # precondition: would clear fallback (>=3)

    await save_monitor_state_async("beh-persist", monitor)

    data = json.loads((tmp_path / "beh-persist_state.json").read_text())
    assert "behavioral_eisv" in data, "save path still dropping behavioral state"
    assert data["behavioral_eisv"]["updates"] == 4

    # Load side (from_dict) restores the persisted block faithfully.
    restored = BehavioralEISV.from_dict(data["behavioral_eisv"])
    assert restored.update_count == 4
    assert restored.confidence >= 0.3  # no longer stuck in fallback after restart


# ─── Fix #4: meta-None guards in process_update_authenticated_async ───────

@pytest.mark.asyncio
async def test_process_update_authenticated_async_tolerates_missing_meta(monkeypatch):
    """Before the fix, a None agent_metadata entry would AttributeError at
    agent_loop_detection.py:507 (pause branch) or :563 (loop_cooldown_until),
    short-circuiting the save call. Guard that regression."""
    from src import agent_loop_detection

    # Force meta to be missing
    monkeypatch.setattr(
        agent_loop_detection, "agent_metadata", {},
    )
    # Make ownership verification always succeed
    monkeypatch.setattr(
        agent_loop_detection, "verify_agent_ownership",
        lambda *a, **kw: (True, ""),
    )
    # Monitor + process_update return a 'pause' decision — triggers the
    # previously-unguarded pause branch at line 507
    fake_monitor = MagicMock()
    fake_monitor.process_update = MagicMock(return_value={
        "decision": {"action": "pause", "reason": "test"},
        "metrics": {"coherence": 0.4},
    })
    # get_or_create_monitor is imported lazily inside the handler; patch at source
    monkeypatch.setattr(
        "src.agent_lifecycle.get_or_create_monitor",
        lambda agent_id: fake_monitor,
    )
    # Skip loop detection
    monkeypatch.setattr(
        agent_loop_detection, "detect_loop_pattern",
        lambda agent_id: (False, ""),
    )
    # Hydration: no-op
    monkeypatch.setattr(
        "src.agent_monitor_state.hydrate_from_db_if_fresh",
        AsyncMock(return_value=False),
    )
    # DB increment: pretend it succeeds
    fake_db = MagicMock()
    fake_db.increment_update_count = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "src.agent_storage.get_db", lambda: fake_db,
    )
    # save_monitor_state_async: track whether we reached it
    save_called = asyncio.Event()

    async def _fake_save(*a, **kw):
        save_called.set()

    monkeypatch.setattr(
        agent_loop_detection, "save_monitor_state_async", _fake_save,
    )

    # Previously raised AttributeError on None.status / None.loop_cooldown_until
    result = await agent_loop_detection.process_update_authenticated_async(
        agent_id="no-meta-agent",
        api_key="dummy",
        agent_state={"task_type": "mixed"},
        auto_save=True,
    )

    # Handler returned, save was reached (previously skipped via exception)
    assert result is not None
    assert save_called.is_set(), "save_monitor_state_async was not reached"
