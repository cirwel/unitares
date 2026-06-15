"""Pin v2-ontology language in tool_descriptions.json for identity-domain tools.

(2026-04-25): tool descriptions
are MCP-canonical and travel to every client (including ones with no skill
surface like claude.ai). Embedding v2-ontology invariants directly in the
descriptions for `onboard` and `identity` is the cheapest way to prevent
Hermes-class derivations of removed-architecture "fixes" (e.g., proposing to
auto-inject continuity_token at the client transport layer).

If these assertions fail, do not loosen them. The Hermes 2026-04-25 incident
is the canonical failure mode; the rule lives here so it survives future edits.
"""

import json
from pathlib import Path

DESCRIPTIONS_PATH = Path(__file__).resolve().parents[1] / "src" / "tool_descriptions.json"


def _load() -> dict:
    return json.loads(DESCRIPTIONS_PATH.read_text())


def test_identity_description_carries_v2_ontology_framing():
    desc = _load()["identity"]
    assert "v2 ontology" in desc, "identity description must reference v2 ontology lineage"
    assert "Identity Honesty Part C" in desc, "identity description must cite Part C (2026-04-18)"
    assert "fresh process-instance is a fresh agent" in desc, (
        "identity description must state the fresh-process-instance invariant"
    )
    assert "parent_agent_id" in desc, (
        "identity description must point at parent_agent_id as the lineage-declaration mechanism"
    )


def test_identity_description_warns_against_continuity_token_auto_injection():
    desc = _load()["identity"]
    assert "ANTI-PATTERN" in desc, "identity description must flag the auto-injection anti-pattern"
    assert "auto-inject continuity_token" in desc.lower() or "auto-inject `continuity_token`" in desc, (
        "identity description must explicitly call out auto-injecting continuity_token between calls"
    )
    assert "transport layer" in desc, (
        "anti-pattern call-out must name the client transport layer (where Hermes proposed the fix)"
    )
    assert "silent-resurrection" in desc, (
        "anti-pattern call-out must name the silent-resurrection vector Part C closed"
    )


def test_identity_description_drops_pre_part_c_recovery_framing():
    """The pre-2026-04-18 description told agents identity() was the recovery
    path 'after context loss', which is exactly the misderivation that produced
    the Hermes 2026-04-25 incident. The new description must not reintroduce it.
    """
    desc = _load()["identity"]
    forbidden = [
        "After context loss: Call identity() to recover your identity",
        "Auto-creates identity if first call",
        "Auto-creates identity if this is your first call",
    ]
    for phrase in forbidden:
        assert phrase not in desc, (
            f"forbidden pre-Part-C phrase reintroduced in identity description: {phrase!r}"
        )


def test_onboard_description_warns_against_continuity_token_auto_injection():
    desc = _load()["onboard"]
    assert "ANTI-PATTERN" in desc, "onboard description must flag the auto-injection anti-pattern"
    assert "auto-inject" in desc.lower() and "continuity_token" in desc, (
        "onboard description must explicitly call out auto-injecting continuity_token"
    )
    assert "transport layer" in desc, (
        "onboard anti-pattern call-out must name the client transport layer"
    )
    assert "Identity Honesty Part C" in desc or "Part C" in desc, (
        "onboard description must cite Part C as the source of the invariant"
    )


def test_onboard_description_keeps_v2_ontology_framing():
    """Pre-existing v2-ontology framing in onboard must survive future edits."""
    desc = _load()["onboard"]
    assert "v2 ontology" in desc, "onboard description must reference v2 ontology"
    assert "fresh process-instance" in desc, (
        "onboard description must state the fresh-process-instance posture"
    )
    assert "parent_agent_id" in desc, (
        "onboard description must point at parent_agent_id as lineage mechanism"
    )


# -----------------------------------------------------------------------------
# Served-description guards.
#
# The assertions above pin tool_descriptions.json, but _IDENTITY_DESCRIPTION_OVERRIDES
# in tool_descriptions.py *shadows* the JSON for `onboard`/`identity` at load
# time — so the override is what actually reaches every MCP client. The JSON
# anti-pattern warning was dormant until restored to the override. These tests
# guard the SERVED text so the Hermes-class safeguard cannot silently drop out
# of what ships again.
# -----------------------------------------------------------------------------

def _served() -> dict:
    from src.tool_descriptions import TOOL_DESCRIPTIONS
    return TOOL_DESCRIPTIONS


def test_served_onboard_description_warns_against_auto_injection():
    desc = _served()["onboard"]
    assert "ANTI-PATTERN" in desc, "served onboard description must flag the auto-injection anti-pattern"
    assert "auto-inject continuity_token" in desc, (
        "served onboard description must call out auto-injecting continuity_token"
    )
    assert "transport layer" in desc, "served onboard anti-pattern must name the client transport layer"
    assert "silent-resurrection" in desc, "served onboard anti-pattern must name the silent-resurrection vector"
    assert "Part C" in desc, "served onboard description must cite Part C as the source of the invariant"


def test_served_identity_description_warns_against_auto_injection():
    desc = _served()["identity"]
    assert "ANTI-PATTERN" in desc, "served identity description must flag the auto-injection anti-pattern"
    assert "auto-inject continuity_token" in desc, (
        "served identity description must call out auto-injecting continuity_token"
    )
    assert "transport layer" in desc, "served identity anti-pattern must name the client transport layer"
    assert "silent-resurrection" in desc, "served identity anti-pattern must name the silent-resurrection vector"
    assert "parent_agent_id" in desc, (
        "served identity description must point at parent_agent_id as the lineage mechanism"
    )
