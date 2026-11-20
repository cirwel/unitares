"""Pin the producer-aware coherence contract in the governance-fundamentals skill.

This check lived only in the plugin repository, which receives `skills/` as a
byte mirror about a day after a change lands here. An edit to the section
therefore passed every check in its own pull request and turned the mirror red
downstream, where its author never saw it (plugin #204, #367, #402). It runs
here so the edit and its source review land in one pull request.
"""

import hashlib
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "governance-fundamentals"
    / "SKILL.md"
)


def _parts() -> tuple[str, str]:
    text = SKILL.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    return frontmatter, body


def _coherence_section_sha() -> str:
    _, body = _parts()
    start = body.index("\n## Coherence\n")
    end = body.index("\n## ", start + 1)
    return hashlib.sha256(body[start + 1 : end].encode("utf-8")).hexdigest()[:16]


def test_coherence_section_is_the_reviewed_text() -> None:
    # Deliberately a frozen literal, not a derived value: this is a tripwire.
    # It pins a hash of the "## Coherence" section only, so it fires when that
    # contract text changes and stays quiet on freshness re-stamps and edits
    # elsewhere in the skill.
    # When it fires: re-read the section against src/coherence_provenance.py,
    # src/behavioral_sensor.py and src/cold_start_risk_confirmation.py, check
    # that the assertions in this file still describe the deployed defaults,
    # then replace the hash and say what was rechecked.
    #
    # Reviewed at 427bdbe5 (#3286), HU1/SD1/SH1 defaults: behavioral authority
    # reads "once behavioral confidence reaches 0.3", matching
    # BEHAVIORAL_AUTHORITY_THRESHOLD in src/cold_start_risk_confirmation.py.
    reviewed = "6adb036f423f7be5"
    assert _coherence_section_sha() == reviewed, (
        "The governance-fundamentals '## Coherence' section changed. Re-read it "
        "against the sources named above this assertion, then update the hash "
        f"to {_coherence_section_sha()} in this pull request."
    )


def test_coherence_sources_and_roles_are_explicit() -> None:
    frontmatter, body = _parts()

    assert "unitares/src/behavioral_sensor.py" in frontmatter
    assert "unitares/src/coherence_provenance.py" in frontmatter
    assert "`legacy_tanh_v`" in body
    assert "`ode_control_feedback`" in body
    assert "`manifold`" in body
    assert "`eis_structural_measurement`" in body


def test_legacy_controller_is_not_taught_as_health_or_balance() -> None:
    _, body = _parts()
    plain_body = body.replace("**", "")

    assert "not a symmetric health/balance score" in plain_body
    assert "Existing critical thresholds are compatibility gates" in body
    assert "Think of it as structural health" not in body
    assert "Coherence reflects balance" not in body


def test_e_and_i_self_loops_are_taught_as_neutralized_by_default() -> None:
    _, body = _parts()

    assert "coherence-level self-loops neutralized by default" in body
    assert "coherence-trend and fleet-strategic self-loops neutralized by default" in body
    assert "UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1" in body
    assert "UNITARES_PER_AGENT_I_CALIBRATION=1" in body
    assert "UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0" in body
    assert "legacy coherence-level term" not in body
    assert "trend of that same legacy controller scalar" not in body
    assert "it feeds E, I," not in body
    assert "omitted-confidence estimator and the compatibility backstops" in body


def test_hidden_confidence_dependency_is_disclosed() -> None:
    frontmatter, body = _parts()

    assert "unitares/src/confidence.py" in frontmatter
    assert "55% of its base weight" in body
    assert "confidence_reliability.coherence_dependency=ode_control_feedback" in body
    assert "not independent confidence evidence" in body
