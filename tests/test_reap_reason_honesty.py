"""The sweeper's failure text must describe the sweep, not invent a story.

Every reaped dialectic session used to record the identical sentence — "Session
auto-resolved: inactive for >120 minutes" — whether it had stalled
mid-negotiation or had been sitting in `awaiting_facilitation` waiting for a
human. Both facts were local variables a few lines above the write.

The cost was real. Reading those rows produces "the agent opened a session and
walked away." The transcripts say the opposite: paused agents stayed alive
31-846 minutes past the verdict, four of six submitted a further synthesis, and
the self-clear guard correctly refused them. The row is the only artifact that
outlives the session, so a false reason there becomes the permanent account.

These tests pin the two properties that matter: the text must reflect observed
state, and it must NOT assert a verdict the sweeper is in no position to know.
"""

from __future__ import annotations

import pytest

from src.mcp_handlers.dialectic.auto_resolve import _describe_reap


def test_facilitation_wait_is_named_as_such():
    """The operator-shaped failure must not read as an agent-shaped one."""
    text = _describe_reap(
        phase="synthesis", awaiting_facilitation=True, idle_seconds=7300
    )
    assert "awaiting human facilitation" in text
    assert "No operator acted" in text
    # The specific misreading this exists to prevent.
    assert "abandoned" in text and "not evidence" in text


def test_ordinary_stall_points_at_the_record_instead_of_guessing():
    text = _describe_reap(
        phase="synthesis", awaiting_facilitation=False, idle_seconds=7300
    )
    assert "read the last synthesis" in text
    assert "facilitation" not in text


@pytest.mark.parametrize("awaiting", [True, False])
def test_never_claims_a_verdict(awaiting):
    """The sweeper does not load the transcript, so it cannot know who was right.

    Replacing one confident wrong sentence with a different confident wrong
    sentence would be no improvement.
    """
    text = _describe_reap(
        phase="synthesis", awaiting_facilitation=awaiting, idle_seconds=100
    )
    assert "not a reviewer verdict" in text
    for overclaim in ("rejected", "disagreed", "refused", "agreed"):
        assert overclaim not in text.lower(), (
            f"the sweep asserted {overclaim!r}, which it cannot observe"
        )


def test_idle_time_is_reported_when_known_and_omitted_when_not():
    with_time = _describe_reap(
        phase="thesis", awaiting_facilitation=False, idle_seconds=7200
    )
    assert "2.0h idle" in with_time

    without = _describe_reap(
        phase="thesis", awaiting_facilitation=False, idle_seconds=None
    )
    assert "idle" not in without
    # Still a usable sentence with the optional parts missing.
    assert without.startswith("Reaped by the inactivity sweep")


def test_missing_phase_degrades_without_rendering_none():
    """`None` in a stored reason reads to a later human as a value."""
    text = _describe_reap(
        phase=None, awaiting_facilitation=False, idle_seconds=None
    )
    assert "None" not in text
    assert "phase" not in text


def test_negative_idle_is_not_reported():
    """Clock skew or a future `updated_at` must not print a negative duration."""
    text = _describe_reap(
        phase="synthesis", awaiting_facilitation=False, idle_seconds=-5
    )
    assert "-" not in text.split("phase")[0]
    assert "idle" not in text
