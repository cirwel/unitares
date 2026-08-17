"""Cross-runtime fingerprint contract for FLEET findings.

Companion to `test_sentinel_forced_release_fingerprint_parity.py`, which covers
the forced-release alarms. This file covers the four `FleetState.analyze()`
findings.

Why the extras exist at all: the legacy 4-part key is
`["sentinel", type, violation_class, <Sentinel's own uuid>]` — it names the
EMITTER, never the subject. `/api/findings` dedups on that string for 1800s
(`src/event_detector.py::_dedup_window_seconds`), so every finding type that can
legitimately emit more than one instance per cycle shared a single bucket and
only the first instance survived the window. Measured 2026-08-16 on
`audit.events`: 14 days of `entropy_outlier` rows spaced 30.0-30.4min apart —
clock-gated by the window, not by the condition — with the subject agent
rotating across each boundary (codex_1668bf5e -> codex_42f07d13 -> Lumen -> ...).
That is silent detection loss, not just duplicate noise.

`fingerprint_extra` appends the discriminator that makes two co-occurring
subjects distinct. It deliberately does NOT include continuously-moving values
(the agent set for coordinated_degradation, the event count for
correlated_events) — keying on those would make every cycle a fresh fingerprint
and turn dedup off entirely, which is the opposite failure.

Same contract style as the forced-release parity suite: this file pins the
PYTHON side to exact literals and
`elixir/sentinel/test/unitares_sentinel/fleet_analysis_test.exs` pins the SAME
literals on the BEAM side. The two suites asserting identical literals IS the
cross-runtime contract — the runtimes must dedup against each other, since a
finding emitted by one must not re-fire from the other.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agents.common.findings import compute_fingerprint  # noqa: E402

SENTINEL_UUID = "sentinel-self-uuid"


def _fingerprint(finding_type: str, violation_class: str, extra: list[str]) -> str:
    """Mirror of the emit-site key in agents/sentinel/agent.py."""
    return compute_fingerprint(
        ["sentinel", finding_type, violation_class, SENTINEL_UUID, *extra]
    )


# --- Golden literals. The BEAM suite asserts these same strings. ------------


def test_entropy_outlier_fingerprint_matches_beam():
    assert _fingerprint("entropy_outlier", "ENT", ["loud-a"]) == "4103a9bc17b90bc4"


def test_coordinated_degradation_fingerprint_matches_beam():
    assert (
        _fingerprint(
            "coordinated_degradation", "CON", ["legacy_tanh_v", "ode_control_feedback"]
        )
        == "dbd36cff80a11f44"
    )


def test_correlated_events_fingerprint_matches_beam():
    assert (
        _fingerprint(
            "correlated_events",
            "BEH",
            ["circuit_breaker_trip", "knowledge_read", "lifecycle_paused"],
        )
        == "bcad93796db4f90e"
    )


# --- Separation properties, asserted on the Python analyze() output. --------


def _snapshot_findings(findings: list[dict], finding_type: str) -> list[dict]:
    return [f for f in findings if f.get("type") == finding_type]


def test_entropy_outliers_carry_the_subject_agent_as_the_discriminator():
    from agents.sentinel.agent import FleetState

    state = FleetState()
    # 9 quiet agents at S=0.1 plus 2 loud at S=1.0 puts BOTH loud agents at z=2.0.
    for index in range(9):
        _ingest(state, f"agent-{index}", f"Agent {index}", entropy=0.1)
    _ingest(state, "loud-a", "Loud A", entropy=1.0)
    _ingest(state, "loud-b", "Loud B", entropy=1.0)

    outliers = _snapshot_findings(state.analyze(self_agent_id=SENTINEL_UUID), "entropy_outlier")
    assert len(outliers) == 2, "both agents should be detected"

    fingerprints = {
        _fingerprint(f["type"], f["violation_class"], f["fingerprint_extra"])
        for f in outliers
    }
    assert len(fingerprints) == 2, (
        "two agents outlying in the same cycle must not share a dedup bucket"
    )


def test_correlated_events_discriminator_excludes_the_count():
    from agents.sentinel.agent import FleetState

    def build(types: list[str]) -> dict:
        now = datetime.now(timezone.utc)
        state = FleetState()
        for index, event_type in enumerate(types):
            state.ingest(
                {
                    "type": event_type,
                    "agent_id": f"agent-{index}",
                    "timestamp": (now - timedelta(seconds=index * 10)).isoformat(),
                }
            )
        found = _snapshot_findings(state.analyze(), "correlated_events")
        assert found, f"expected a correlated_events finding for {types}"
        return found[0]

    three = build(["knowledge_read", "knowledge_write", "knowledge_read"])
    many = build(["knowledge_read"] * 8 + ["knowledge_write"] * 4)

    assert three["fingerprint_extra"] == many["fingerprint_extra"], (
        "count moves every cycle; keying on it would turn dedup off entirely"
    )

    incident = build(["circuit_breaker_trip", "lifecycle_paused", "knowledge_read"])
    assert incident["fingerprint_extra"] != three["fingerprint_extra"], (
        "a circuit_breaker_trip burst must not dedup behind routine knowledge traffic"
    )


def _ingest(state, agent_id: str, name: str, *, entropy: float) -> None:
    state.ingest(
        {
            "type": "eisv_update",
            "agent_id": agent_id,
            "agent_name": name,
            "eisv": {"E": 0.5, "I": 0.5, "S": entropy, "V": 0.5},
            "coherence": 0.9,
            "verdict": "proceed",
        }
    )
