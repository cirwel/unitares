"""Resident registration must be legible at the moment it is decided.

Registering a resident has two halves that nothing used to connect: the name
must be on the deployment's ``UNITARES_RESIDENTS`` roster, AND the privileged
tags (``persistent``, ``autonomous``) are granted only by the onboard
classifier, only at mint, only on an exact roster match.

Before 2026-08-26 an off-roster name minted an ``ephemeral`` identity that
reported success. The SDK's ``persistent=True`` could not repair it — those
tags are in ``PRIVILEGED_TAGS`` and the server refuses self-assignment — so
the agent logged a tag-reconcile warning every cycle, forever, without ever
naming the cause, and the orphan sweep archived it days later.

These pin the verdict the mint response now carries.
"""
from __future__ import annotations

import pytest

from src.grounding.onboard_classifier import (
    RESIDENT_DEFAULT_TAGS,
    resident_registration,
)

ROSTER = frozenset({"Vigil", "Sentinel", "Doctor"})


def test_roster_name_reports_registered():
    got = resident_registration("Doctor", ["persistent", "autonomous"], roster=ROSTER)
    assert got["status"] == "registered"
    assert got["on_roster"] is True
    assert sorted(got["granted_tags"]) == sorted(RESIDENT_DEFAULT_TAGS)


def test_off_roster_name_is_named_as_such_with_the_remedy():
    """⛔The silent failure this exists to end.

    The detail must name the env var and say the identity cannot be upgraded
    in place, because that is the part nobody re-derives.
    """
    got = resident_registration("MyResident", ["ephemeral"], roster=ROSTER)
    assert got["status"] == "not_on_roster"
    assert got["on_roster"] is False
    assert got["granted_tags"] == ["ephemeral"]
    assert "UNITARES_RESIDENTS" in got["detail"]
    assert "fresh identity" in got["detail"]


def test_empty_roster_is_a_posture_not_an_error():
    """A residentless install is the shipped default and must not be nagged.

    ⛔Reporting `not_on_roster` here would tell every fresh install it had
    misconfigured something, when the empty roster is exactly right.
    """
    got = resident_registration("Anything", ["ephemeral"], roster=frozenset())
    assert got["status"] == "no_roster_configured"
    assert got["roster_configured"] is False
    assert "default" in got["detail"]


def test_caller_supplied_tags_are_not_overridden_or_misreported():
    """`None` from the stamp means SKIPPED, and is not the same as `[]`."""
    got = resident_registration("Custom", None, roster=ROSTER)
    assert got["status"] == "caller_supplied_tags"
    assert got["granted_tags"] == []


def test_anonymous_mint_gets_no_verdict():
    """No name means no registration was attempted — silence, not a failure."""
    assert resident_registration(None, ["ephemeral"], roster=ROSTER) is None
    assert resident_registration("", ["ephemeral"], roster=ROSTER) is None


def test_partial_grant_is_not_registered():
    """`persistent` alone still leaves loop-detection pattern 4 free to
    starve the resident's state writes (Steward 2026-04-20)."""
    got = resident_registration("Doctor", ["persistent"], roster=ROSTER)
    assert got["status"] != "registered"


def test_verdict_never_names_a_fleet_identity():
    """⛔Fleet-agnostic: the response reflects the deployment's own roster and
    must never leak this maintainer's resident names into the framework."""
    got = resident_registration("Whatever", ["ephemeral"], roster=frozenset())
    blob = repr(got)
    for name in ("Lumen", "Vigil", "Sentinel", "Watcher", "Steward", "Chronicler"):
        assert name not in blob


def test_payload_is_json_safe():
    import json
    json.dumps(resident_registration("Doctor", ["persistent", "autonomous"], roster=ROSTER))


class TestPublicPromise:
    """The SDK README advertises this path; it must not promise protection
    that depends on an undocumented prerequisite."""

    @pytest.fixture
    def readme(self):
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / "agents" / "sdk" / "README.md"
        return p.read_text()

    def test_readme_documents_the_roster_prerequisite(self, readme):
        assert "UNITARES_RESIDENTS" in readme

    def test_readme_does_not_claim_bare_auto_archive_protection(self, readme):
        """⛔The original line read `persistent=True  # protects from
        auto-archive`, which is false for any name not on the roster — and the
        roster was not mentioned anywhere on the page."""
        assert "# protects from auto-archive" not in readme
