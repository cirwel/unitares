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


def test_process_update_description_distinguishes_session_id_from_token():
    desc = _load()["process_agent_update"]
    assert "client_session_id (string): In-session binding identifier" in desc
    assert "not a continuity_token or cross-process proof" in desc
    assert "client_session_id (string): Session continuity token" not in desc


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


def test_checked_in_json_identity_description_never_teaches_a_bare_read():
    """The same rule, against the file rather than the merged dict.

    `_load_descriptions()` overlays `_IDENTITY_DESCRIPTION_OVERRIDES` on top of
    `tool_descriptions.json`, so a test that reads TOOL_DESCRIPTIONS cannot see
    what the JSON says. The file ships as packaged data and is a teaching
    surface in its own right, so it went on telling readers to call
    `identity()` with no parameters while the served text said the opposite.
    """
    import json
    from pathlib import Path

    import src.tool_descriptions as td

    raw = json.loads(Path(td._DESCRIPTIONS_FILE).read_text(encoding="utf-8"))
    desc = raw["identity"]
    assert "identity() anytime" not in desc, (
        "checked-in identity description must not teach an argument-less call as a read"
    )
    assert "Check identity (no parameters)" not in desc, (
        "checked-in identity description must not offer a no-parameter example"
    )
    assert "client_session_id" in desc


def test_served_identity_description_never_teaches_a_bare_read():
    """The served description must not tell a client that bare identity() reads.

    `handle_identity_adapter` gates a call with no proof signal to
    `force_new=true` (#156, 2026-04-25), so an argument-less `identity()`
    mints and persists a new agent and then reports on *that* one. A
    description that calls it a way to see your current binding sends every
    client down a path the server closed. This is the check that was missing
    when the description carried both the warning and the contradiction at
    once.
    """
    desc = _served()["identity"]
    assert "Use identity() with no arguments" not in desc, (
        "served identity description must not teach an argument-less call as a read"
    )
    assert "identity(client_session_id=" in desc, (
        "served identity description must name the argument that makes the answer yours"
    )
    assert "mints" in desc or "fresh mint" in desc, (
        "served identity description must say what an argument-less call does instead"
    )


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
