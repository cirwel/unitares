"""§7.13.6 PR 3 — class-aware void threshold (interim safety net).

Pins the API surface added by PR 3:

  - `config.get_void_threshold(history, adaptive=True, agent_class=None)`
    returns the class-specific override when agent_class is in
    VOID_THRESHOLD_BY_CLASS, else the existing adaptive/default behavior.

  - `monitor_void.check_void_state(state, agent_class=None)` threads
    agent_class through. Reads `state.agent_class` as a fallback.

  - `governance_monitor.check_void_state` resolves the agent class lazily
    via `_resolve_agent_class` and caches it. Defends against failures so
    behavior degrades to default — never raises.

The interim safety net's job: a resident-class agent with V_ss = 0.19 does
NOT trip check_void_state, while a non-resident at the same V_ss DOES trip.
0.19 = the live-measured Steward V_ss from the 2026-05-01 incident memory
(`project_steward-paused.md`). 0.30 (the resident override) clears it;
0.15 (default INITIAL) does not.

Spec: §7.13.6 PR 3
      memory: project_steward-paused.md
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.governance_config import config  # noqa: E402
from src.monitor_void import check_void_state  # noqa: E402


# ---------- get_void_threshold direct tests ----------


def test_get_void_threshold_no_agent_class_uses_adaptive_default():
    """No agent_class kwarg → existing adaptive behavior unchanged."""
    history = np.array([0.05] * 100)
    threshold = config.get_void_threshold(history, adaptive=True)
    # Adaptive threshold for low-variance history clamps to MIN.
    assert threshold == config.VOID_THRESHOLD_MIN


def test_get_void_threshold_resident_class_returns_override():
    """Each known resident class returns 0.30 regardless of history shape."""
    history = np.array([0.05] * 100)
    for cls in (
        "Lumen", "Vigil", "Sentinel", "Watcher", "Steward", "Chronicler",
        "embodied", "resident_persistent",
    ):
        threshold = config.get_void_threshold(history, adaptive=True, agent_class=cls)
        assert threshold == 0.30, f"class {cls!r} should override to 0.30"


def test_get_void_threshold_unknown_class_falls_through_to_adaptive():
    """An agent_class not in VOID_THRESHOLD_BY_CLASS doesn't override."""
    history = np.array([0.05] * 100)
    threshold = config.get_void_threshold(history, adaptive=True, agent_class="default")
    assert threshold == config.VOID_THRESHOLD_MIN  # adaptive path took over

    threshold_eph = config.get_void_threshold(history, adaptive=True, agent_class="ephemeral")
    assert threshold_eph == config.VOID_THRESHOLD_MIN


def test_get_void_threshold_override_ignores_adaptive_flag():
    """The class override returns the per-class value even when adaptive=False —
    the override is intentional, not derived from the adaptive window."""
    history = np.array([0.05] * 100)
    assert config.get_void_threshold(history, adaptive=False, agent_class="Steward") == 0.30


# ---------- check_void_state integration ----------


class _FakeState:
    """Minimal duck-type for monitor_void.check_void_state."""

    def __init__(self, V: float, V_history: list[float] | None = None,
                 agent_class: str | None = None):
        self.V = V
        self.V_history = V_history if V_history is not None else [V]
        self.void_active = False
        if agent_class is not None:
            self.agent_class = agent_class


def test_check_void_state_resident_class_kwarg_at_steward_v_ss_does_not_trip():
    """The 2026-05-01 incident: Steward V_ss = 0.19 tripped void_active under the
    standard 0.15 threshold. With agent_class='Steward' the threshold becomes
    0.30 — V_ss = 0.19 no longer trips."""
    state = _FakeState(V=0.19)
    void = check_void_state(state, agent_class="Steward")
    assert void is False
    assert state.void_active is False


def test_check_void_state_default_at_steward_v_ss_does_trip():
    """Same V_ss without the class kwarg — proves the override is what
    flips the outcome, not a baseline change."""
    state = _FakeState(V=0.19)
    void = check_void_state(state)  # no agent_class
    assert void is True
    assert state.void_active is True


def test_check_void_state_resident_class_via_state_attribute_also_works():
    """Per the resolution order in monitor_void: kwarg → state.agent_class → None.
    Populating state.agent_class is equivalent to passing the kwarg."""
    state = _FakeState(V=0.19, agent_class="Sentinel")
    void = check_void_state(state)  # no kwarg
    assert void is False


def test_check_void_state_kwarg_takes_precedence_over_state_attribute():
    """kwarg wins. State carries 'default' (no override); kwarg passes
    'Steward'. Should use the resident threshold, not fall through."""
    state = _FakeState(V=0.19, agent_class="default")
    void = check_void_state(state, agent_class="Steward")
    assert void is False


def test_check_void_state_extreme_v_still_trips_for_resident():
    """The override widens the threshold but doesn't disable it. V well past
    0.30 still trips even for residents — the safety net is widening, not
    removal."""
    state = _FakeState(V=0.5)
    void = check_void_state(state, agent_class="Steward")
    assert void is True


# ---------- governance_monitor wrapper integration ----------


def test_governance_monitor_resolves_resident_agent_id_to_class():
    """When agent_id is literally 'Steward' (the historical pattern for some
    in-process residents), governance_monitor's _resolve_agent_class finds
    it via the KNOWN_RESIDENT_LABELS short-circuit."""
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor(agent_id="Steward", load_state=False)
    resolved = monitor._resolve_agent_class()
    assert resolved == "Steward"


def test_governance_monitor_resolves_non_resident_to_none():
    """Plain UUID / unrecognized agent_id with no state.agent_class AND no
    metadata cache entry → returns None, which leaves the threshold on its
    default adaptive path."""
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor(agent_id="abcdefab-cdef-4abc-8def-abcdefabcdef",
                                load_state=False)
    resolved = monitor._resolve_agent_class()
    assert resolved is None


def test_governance_monitor_uuid_resolves_via_agent_metadata_cache():
    """Production case (regression for 2026-05-04 canary): agent_id is a UUID,
    label/tags live in agent_metadata cache. _resolve_agent_class MUST consult
    the cache to find the resident class — without this the void-threshold
    override never applied to UUID-keyed residents and the canary tripped
    void_pause anyway. Caught on Steward unpause: V=0.081 with default
    adaptive threshold (clamped to MIN=0.10) → void_active=true → pause.
    Resident threshold (0.30) would have cleared it."""
    from src.agent_metadata_model import AgentMetadata, agent_metadata
    from src.governance_monitor import UNITARESMonitor

    # Steward's actual production UUID per project_steward-paused.md memory.
    steward_uuid = "9a6681ec-1d16-4143-ada9-282f14483fea"

    # Simulate a populated cache entry (what background_metadata_load produces
    # in production for Steward).
    agent_metadata[steward_uuid] = AgentMetadata(
        agent_id=steward_uuid,
        status="active",
        created_at="2026-01-01T00:00:00+00:00",
        last_update="2026-05-04T03:00:00+00:00",
        label="Steward",
        tags=["persistent", "autonomous"],
    )

    try:
        monitor = UNITARESMonitor(agent_id=steward_uuid, load_state=False)
        resolved = monitor._resolve_agent_class()
        assert resolved == "Steward", (
            f"UUID-form Steward must resolve to 'Steward' class via cache; "
            f"got {resolved!r}. Without this lookup, the v0.11.3 PR 3 "
            f"safety net is a no-op for production residents."
        )
    finally:
        agent_metadata.pop(steward_uuid, None)


def test_governance_monitor_uuid_with_persistent_autonomous_tags_resolves():
    """Tag-derived resident classification path: agents with no label-match
    but tags={persistent, autonomous} resolve to 'resident_persistent' via
    classify_agent's tag fallback."""
    from src.agent_metadata_model import AgentMetadata, agent_metadata
    from src.governance_monitor import UNITARESMonitor

    uuid = "11111111-2222-4333-8444-555555555555"
    agent_metadata[uuid] = AgentMetadata(
        agent_id=uuid,
        status="active",
        created_at="2026-01-01T00:00:00+00:00",
        last_update="2026-05-04T03:00:00+00:00",
        label=None,  # No KNOWN_RESIDENT_LABELS match
        tags=["persistent", "autonomous"],
    )

    try:
        monitor = UNITARESMonitor(agent_id=uuid, load_state=False)
        resolved = monitor._resolve_agent_class()
        assert resolved == "resident_persistent"
    finally:
        agent_metadata.pop(uuid, None)


def test_governance_monitor_state_agent_class_takes_precedence():
    """If upstream loaders populate state.agent_class, that wins over the
    agent_id-label fallback. Lets the real production wiring opt in to a
    class without relying on agent_id == label."""
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor(agent_id="some-uuid", load_state=False)
    monitor.state.agent_class = "Vigil"
    resolved = monitor._resolve_agent_class()
    assert resolved == "Vigil"


def test_governance_monitor_caches_resolved_class():
    """_resolve_agent_class is called once and cached on the instance — the
    hot path of check_void_state shouldn't re-import / re-derive each cycle."""
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor(agent_id="Steward", load_state=False)
    # GovernanceState's E/I/S/V are read-only properties backed by
    # unitaires_state — set V via the underlying state directly. Keeping the
    # test surgical (we're testing _resolve_agent_class caching, not the
    # state-mutation API).
    monitor.state.unitaires_state.V = 0.19
    monitor.state.V_history = [0.19]
    monitor.check_void_state()
    assert monitor._resolved_agent_class == "Steward"
    # Second call uses cache: changing agent_id post-init doesn't re-resolve.
    monitor.agent_id = "DifferentName"
    assert monitor._resolved_agent_class == "Steward"


def test_governance_monitor_check_void_state_residents_exempt_at_steward_v_ss():
    """End-to-end: a Steward-ish monitor instance with V_ss = 0.19 doesn't
    trip void_active — the API surface ties through correctly."""
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor(agent_id="Steward", load_state=False)
    monitor.state.unitaires_state.V = 0.19
    monitor.state.V_history = [0.19]
    void = monitor.check_void_state()
    assert void is False
    assert monitor.state.void_active is False


def test_governance_monitor_check_void_state_non_resident_trips_at_same_v_ss():
    """Control: non-resident monitor at the same V_ss DOES trip."""
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor(agent_id="abc-not-a-resident", load_state=False)
    monitor.state.unitaires_state.V = 0.19
    monitor.state.V_history = [0.19]
    void = monitor.check_void_state()
    assert void is True
    assert monitor.state.void_active is True
