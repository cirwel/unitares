"""Every emitted decision string must have a behavioral score.

`governance_monitor.py` appends `decision.get('sub_action', decision['action'])`
to `decision_history`, so what reaches `behavioral_sensor._DECISION_SCORES` is
the *sub_action* vocabulary. Five of the six emitted sub_actions had no entry
and fell through to the 0.5 neutral default — meaning every pause and block
variant the system actually produces was scored as "nothing notable happened"
by the E computation that carries verdict authority.

The defect was invisible because a missing key and a genuinely middling decision
render identically: both are 0.5. This test makes the absence loud. It reads the
sub_action literals out of monitor_decision.py rather than hardcoding a list, so
adding a new pause variant without scoring it fails here instead of silently
neutralising itself in production.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.behavioral_sensor import _DECISION_SCORES, _compute_E

REPO = Path(__file__).resolve().parents[1]
MONITOR_DECISION = REPO / "src" / "monitor_decision.py"

# Sub-actions that deliberately carry no score: none today. Kept as an explicit
# seam so a future exemption has to be written down and justified rather than
# achieved by forgetting.
EXEMPT: set[str] = set()


def _emitted_sub_actions() -> set[str]:
    text = MONITOR_DECISION.read_text(encoding="utf-8")
    return set(re.findall(r"""['"]sub_action['"]\s*:\s*['"]([a-z_]+)['"]""", text))


def test_source_of_truth_is_readable():
    """Guard the guard: if the regex stops matching, this test would pass vacuously."""
    emitted = _emitted_sub_actions()
    assert emitted, (
        f"no 'sub_action' literals found in {MONITOR_DECISION} — the extraction "
        "broke, so the coverage assertion below would pass without checking anything"
    )
    assert "risk_pause" in emitted, (
        "expected risk_pause among the emitted sub_actions; extraction is likely wrong"
    )


def test_every_emitted_sub_action_has_a_score():
    missing = sorted(_emitted_sub_actions() - set(_DECISION_SCORES) - EXEMPT)
    assert not missing, (
        f"sub_action(s) {missing} are emitted by monitor_decision.py but have no "
        f"entry in behavioral_sensor._DECISION_SCORES. They would score 0.5 "
        f"(the neutral default) instead of their real value — a pause read as "
        f"'nothing notable happened'. Add them to _DECISION_SCORES."
    )


@pytest.mark.parametrize(
    "sub_action",
    ["risk_pause", "basin_pause", "coherence_pause", "void_pause", "cirs_block"],
)
def test_pause_variants_score_as_pauses_not_neutral(sub_action):
    """A pause must pull E down, not leave it at the unknown-string default."""
    assert _DECISION_SCORES[sub_action] == 0.0


def test_pauses_lower_E_relative_to_approvals():
    """End-to-end: the fix has to move the number, not just the table."""
    coherence = [0.5] * 10
    healthy = _compute_E(["approve"] * 10, coherence)
    paused = _compute_E(["risk_pause"] * 10, coherence)
    assert paused < healthy, "a history of pauses must score below a history of approvals"

    # And it must beat the pre-fix behaviour, where an unknown string defaulted
    # to 0.5 and a sustained pause looked merely average.
    unknown_default = _compute_E(["some_unmapped_action"] * 10, coherence)
    assert paused < unknown_default, (
        "scored pauses must sit below the unknown-string default, otherwise the "
        "table entry is not doing anything"
    )
