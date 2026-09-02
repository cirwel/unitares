"""v2 bilateral attestation must survive a PostgreSQL round trip.

Two defects made a genuine bilateral resolution report as unverifiable once
the session left memory:

1. ``_reconstruct_session_from_dict`` did not pass ``signature_version``
   through, so every reloaded v2 resolution decoded as legacy v1 and
   ``verify_signatures()`` returned False by construction.
2. The read path strips surrounding whitespace from conditions and drops
   empty entries, while ``canonical_payload()`` signed the raw list, so a
   resolution finalized with a padded condition verified against a different
   payload after reload.

These tests pin the fixes and, just as deliberately, what the fixes do not
change: stored conditions, the hard-limit gate's view of them, and the bytes
signed over clean rows.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import (  # noqa: E402
    DialecticMessage,
    DialecticPhase,
    DialecticSession,
    Resolution,
    canonical_conditions,
)
from src.mcp_handlers.dialectic.session import (  # noqa: E402
    _coerce_signature_version,
    _reconstruct_session_from_dict,
)


def _converged_session(conditions):
    s = DialecticSession(
        paused_agent_id="agent-a",
        reviewer_agent_id="agent-b",
        session_type="recovery",
    )
    s.phase = DialecticPhase.RESOLVED
    s.synthesis_round = 2
    now = datetime.now(timezone.utc).isoformat()
    s.transcript.append(DialecticMessage(
        phase="thesis", agent_id="agent-a", timestamp=now,
        root_cause="initial cause", proposed_conditions=["c1"], reasoning="initial",
    ))
    s.transcript.append(DialecticMessage(
        phase="antithesis", agent_id="agent-b", timestamp=now,
        reasoning="counter", concerns=["c"],
    ))
    for agent in ("agent-a", "agent-b"):
        s.transcript.append(DialecticMessage(
            phase="synthesis", agent_id=agent, timestamp=now,
            proposed_conditions=list(conditions), root_cause="agreed cause",
            reasoning=f"from {agent}", agrees=True,
        ))
    return s


def _reload(session, resolution_dict):
    doc = {
        "paused_agent_id": session.paused_agent_id,
        "reviewer_agent_id": session.reviewer_agent_id,
        "paused_agent_state": {},
        "phase": "resolved",
        "resolution": json.loads(json.dumps(resolution_dict)),  # JSONB round trip
    }
    reloaded = _reconstruct_session_from_dict(session.session_id, doc)
    assert reloaded is not None and reloaded.resolution is not None
    return reloaded


# ── defect 1: signature_version dropped on reload ──────────────────────────


def test_reload_preserves_signature_version_and_bilateral_verification():
    s = _converged_session(["agreed"])
    res = s.finalize_resolution("key-a", "key-b")
    assert res.signature_version == 2
    assert res.verify_signatures("key-a", "key-b") is True

    reloaded = _reload(s, res.to_dict())
    assert reloaded.resolution.signature_version == 2
    assert reloaded.resolution.verify_signatures("key-a", "key-b") is True


def test_reload_of_legacy_row_defaults_to_v1_and_stays_unverifiable():
    doc = {
        "paused_agent_id": "agent-a",
        "reviewer_agent_id": "agent-b",
        "paused_agent_state": {},
        "phase": "resolved",
        "resolution": {
            "action": "resume", "conditions": ["c"], "root_cause": "r", "reasoning": "x",
            "signature_a": "sa", "signature_b": "sb", "timestamp": "2026-01-15T12:30:00",
        },
    }
    reloaded = _reconstruct_session_from_dict("sess-legacy", doc)
    assert reloaded.resolution.signature_version == 1
    assert reloaded.resolution.verify_signatures("key-a", "key-b") is False


@pytest.mark.parametrize("raw,expected", [
    (None, 1), (2, 2), ("2", 2), (1, 1), ("garbage", 1), ([], 1), (2.0, 2),
])
def test_coerce_signature_version(raw, expected):
    assert _coerce_signature_version(raw) == expected


# ── defect 2: padded conditions signed one payload, verified another ───────


def test_padded_and_empty_conditions_survive_reload_verification():
    s = _converged_session(["agreed", "  padded  ", ""])
    res = s.finalize_resolution("key-a", "key-b")
    assert res.verify_signatures("key-a", "key-b") is True

    reloaded = _reload(s, res.to_dict())
    # The read path strips and drops empties; the signature was computed over
    # exactly that shape, so it still verifies.
    assert reloaded.resolution.conditions == ["agreed", "padded"]
    assert reloaded.resolution.verify_signatures("key-a", "key-b") is True


def test_stored_conditions_are_not_modified_by_signing():
    """Only the signed bytes are canonicalized. What finalize stores, and what
    check_hard_limits therefore sees, is unchanged by this fix."""
    s = _converged_session(["agreed", "  padded  ", ""])
    res = s.finalize_resolution("key-a", "key-b")
    assert "  padded  " in res.conditions
    assert "" in res.conditions
    # The hard-limit gate evaluates the same raw list it always did.
    assert s.check_hard_limits(res)[0] is True
    whitespace_only = _converged_session(["   "]).finalize_resolution("key-a", "key-b")
    # _merge_proposals keeps each party's entry; the point here is only that
    # the raw whitespace survives finalize untouched.
    assert whitespace_only.conditions and all(c == "   " for c in whitespace_only.conditions)


def test_clean_rows_sign_byte_identical_payloads():
    """Strip-and-drop is the identity on clean conditions, so nothing signed
    before this change over clean rows is affected."""
    res = Resolution(
        action="resume", conditions=["b cond", "a cond"], root_cause="r",
        reasoning="x", signature_a="", signature_b="", timestamp="t",
        signature_version=2,
    )
    legacy_formula = json.dumps({
        "action": "resume",
        "conditions": sorted(["b cond", "a cond"]),
        "root_cause": "r",
        "reasoning": "x",
        "timestamp": "t",
    }, sort_keys=True).encode("utf-8")
    assert res.canonical_payload() == legacy_formula


def test_canonical_conditions_mirrors_read_path_shape():
    assert canonical_conditions(["  a ", "", "b", 3, "   "]) == ["a", "b", "3"]
    assert canonical_conditions(None) == []
    assert canonical_conditions([]) == []
