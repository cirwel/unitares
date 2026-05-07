"""
Tier 2 cross-runtime contract test — Wave 1 Surface 1 (RFC v0.1.2 §C4).

The BEAM Sentinel writes `.sentinel_state.beam` via
`UnitaresSentinel.CycleState.save/2` (elixir/sentinel/lib/unitares_sentinel/cycle_state.ex).
On rollback or operator intervention, the Python Sentinel may need to
read whatever BEAM last wrote. If Python's `load_state` ever fails to
recover the cursor from a BEAM-written file, the de-dup fence regresses
and the alarm replay storm condition (RFC v0.1.1 §Surface 1 rollback
procedure step 4) fires.

The fixture at `elixir/sentinel/test/fixtures/sentinel_state_beam_v1.json`
is byte-equivalent to what `CycleState.save/2` produces today. Drift
between this fixture and BEAM's writer is a CI failure on the BEAM side
(see `cycle_state_test.exs` "Tier 2: BEAM CycleState.load round-trips
a Python-written fixture" for the symmetric direction).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

BEAM_FIXTURE = (
    project_root
    / "elixir"
    / "sentinel"
    / "test"
    / "fixtures"
    / "sentinel_state_beam_v1.json"
)


def test_beam_fixture_exists_and_decodes():
    """The BEAM-written fixture committed to the tree must parse as JSON."""
    assert BEAM_FIXTURE.exists(), (
        f"Tier 2 contract: missing BEAM fixture at {BEAM_FIXTURE}. "
        "Regenerate via `mix run -e 'UnitaresSentinel.CycleState.save(%{...}, "
        "path: \"test/fixtures/sentinel_state_beam_v1.json\")'`."
    )
    decoded = json.loads(BEAM_FIXTURE.read_text())
    assert isinstance(decoded, dict), (
        f"BEAM fixture must decode to a dict; got {type(decoded).__name__}"
    )


def test_python_load_state_recovers_cursor_from_beam_fixture(tmp_path, monkeypatch):
    """
    Python's load_state at agents/sentinel/agent.py:492 MUST recover
    the cursor from a BEAM-written file. This is the symmetric Tier 2
    contract from RFC v0.1.2 §C4. If this fails, BEAM-on-rollback breaks
    Python's de-dup fence and triggers alarm replay.
    """
    # Stage the BEAM fixture as the canonical state file (rollback scenario:
    # BEAM was the canonical writer mid-shadow, then operator unloads BEAM
    # and reloads Python — Python must read whatever's there).
    state_file = tmp_path / ".sentinel_state"
    state_file.write_bytes(BEAM_FIXTURE.read_bytes())

    from agents.sentinel import agent as sentinel_agent

    monkeypatch.setattr(sentinel_agent, "STATE_FILE", state_file)

    # load_state references the module-level STATE_FILE, not `self`, so we
    # can invoke it directly off the class without instantiating the agent
    # (which would pull in WS, SDK, identity, etc.).
    state = sentinel_agent.SentinelAgent.load_state(None)

    cursor = state.get("forced_release_alarm", {}).get("last_event_ts")
    assert cursor is not None, (
        f"Python load_state failed to recover cursor from BEAM fixture: state={state!r}"
    )
    assert isinstance(cursor, str), (
        f"cursor must be a string (ISO-8601), got {type(cursor).__name__}"
    )
    # Loose ISO-8601 shape check; the BEAM writer's exact format is the
    # contract, but we don't pin a specific timestamp here so the fixture
    # can be regenerated without breaking this test.
    assert "T" in cursor and ("+" in cursor or "Z" in cursor), (
        f"cursor must be ISO-8601 with timezone offset, got: {cursor!r}"
    )
