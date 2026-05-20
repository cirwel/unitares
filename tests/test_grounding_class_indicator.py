"""Tests for the class indicator that maps agent metadata to calibration class."""
import pytest
from types import SimpleNamespace

from src.grounding.class_indicator import (
    classify_agent,
    classify_by_label_and_tags,
    KNOWN_RESIDENT_LABELS,
    CLASS_EMBODIED,
    CLASS_RESIDENT_PERSISTENT,
    CLASS_EPHEMERAL,
    CLASS_ENGAGED_EPHEMERAL,
    CLASS_DEFAULT,
)


def _meta(label=None, tags=None):
    return SimpleNamespace(label=label, tags=tags or [])


def test_none_meta_returns_default():
    assert classify_agent(None) == CLASS_DEFAULT


def test_known_resident_labels_return_themselves():
    for label in KNOWN_RESIDENT_LABELS:
        assert classify_agent(_meta(label=label)) == label


def test_embodied_tag_takes_precedence_over_persistent():
    """Lumen has both 'embodied' and 'persistent' but should classify as embodied."""
    assert classify_agent(_meta(tags=["embodied", "persistent"])) == CLASS_EMBODIED


def test_resident_persistent_for_autonomous_persistent():
    assert (
        classify_agent(_meta(tags=["autonomous", "persistent"]))
        == CLASS_RESIDENT_PERSISTENT
    )


def test_ephemeral_tag_classifies_ephemeral():
    assert classify_agent(_meta(tags=["ephemeral"])) == CLASS_EPHEMERAL


def test_unknown_tags_fall_through_to_default():
    assert classify_agent(_meta(tags=["random", "tag"])) == CLASS_DEFAULT


def test_no_tags_no_label_returns_default():
    assert classify_agent(_meta()) == CLASS_DEFAULT


def test_label_overrides_tags_for_known_residents():
    """A 'Vigil' label wins even if tags say something else."""
    assert classify_agent(_meta(label="Vigil", tags=["embodied"])) == "Vigil"


def test_unknown_label_falls_through_to_tag_resolution():
    """An unknown label doesn't short-circuit tag resolution."""
    assert (
        classify_agent(_meta(label="random_session_agent", tags=["ephemeral"]))
        == CLASS_EPHEMERAL
    )


def test_meta_without_tags_attribute_is_safe():
    class Bare:
        label = "Lumen"
    assert classify_agent(Bare()) == "Lumen"


# S8a Phase-2 additions: engaged_ephemeral is the post-promotion class for
# previously-ephemeral identities with total_updates >= 3.

def test_engaged_ephemeral_tag_classifies_engaged_ephemeral():
    assert classify_agent(_meta(tags=["engaged_ephemeral"])) == CLASS_ENGAGED_EPHEMERAL


def test_engaged_ephemeral_takes_precedence_over_ephemeral_in_race():
    """If both tags are present (e.g. concurrent write during the Phase-2
    sweep), the post-promotion class wins. Defense-in-depth — the sweep
    UPDATE removes ephemeral atomically, so this state is not expected."""
    assert (
        classify_agent(_meta(tags=["ephemeral", "engaged_ephemeral"]))
        == CLASS_ENGAGED_EPHEMERAL
    )


def test_embodied_still_takes_precedence_over_engaged_ephemeral():
    """Lumen-style metadata with both embodied and (somehow) engaged_ephemeral
    should remain embodied — embodied is a structural class, engaged_ephemeral
    is a session-shape class."""
    assert (
        classify_agent(_meta(tags=["embodied", "engaged_ephemeral"]))
        == CLASS_EMBODIED
    )


def test_engaged_ephemeral_does_not_collide_with_resident_persistent():
    """An agent tagged engaged_ephemeral + persistent + autonomous shouldn't
    happen in practice (they're distinct lifecycles), but if it does the
    engaged_ephemeral wins since it's the more specific shape signal."""
    assert (
        classify_agent(_meta(tags=["engaged_ephemeral", "persistent", "autonomous"]))
        == CLASS_ENGAGED_EPHEMERAL
    )


# Canonical label-and-tags fold: batch scripts and the runtime path must
# agree byte-for-byte. The runtime adapter (classify_agent) goes through
# this function; these tests pin the raw entry point that calibrate and
# verdict_counterfactual now import directly, so regressions in either
# direction show up here.


class TestClassifyByLabelAndTags:
    def test_none_label_none_tags_returns_default(self):
        assert classify_by_label_and_tags(None, None) == CLASS_DEFAULT

    def test_none_label_empty_tags_returns_default(self):
        assert classify_by_label_and_tags(None, []) == CLASS_DEFAULT

    def test_known_resident_labels_return_themselves(self):
        for label in KNOWN_RESIDENT_LABELS:
            assert classify_by_label_and_tags(label, []) == label

    def test_known_label_overrides_tags(self):
        assert classify_by_label_and_tags("Vigil", ["embodied"]) == "Vigil"

    def test_unknown_label_falls_through_to_tags(self):
        assert (
            classify_by_label_and_tags("random_session_agent", ["ephemeral"])
            == CLASS_EPHEMERAL
        )

    def test_embodied_tag(self):
        assert classify_by_label_and_tags(None, ["embodied"]) == CLASS_EMBODIED

    def test_engaged_ephemeral_tag(self):
        assert (
            classify_by_label_and_tags(None, ["engaged_ephemeral"])
            == CLASS_ENGAGED_EPHEMERAL
        )

    def test_ephemeral_tag(self):
        assert classify_by_label_and_tags(None, ["ephemeral"]) == CLASS_EPHEMERAL

    def test_resident_persistent_requires_both_tags(self):
        assert (
            classify_by_label_and_tags(None, ["autonomous", "persistent"])
            == CLASS_RESIDENT_PERSISTENT
        )
        assert classify_by_label_and_tags(None, ["persistent"]) == CLASS_DEFAULT
        assert classify_by_label_and_tags(None, ["autonomous"]) == CLASS_DEFAULT

    def test_engaged_ephemeral_beats_ephemeral_in_race(self):
        """Eliminating the historical drift: the legacy script fold returned
        'ephemeral' here. Canonical fold returns engaged_ephemeral."""
        assert (
            classify_by_label_and_tags(None, ["ephemeral", "engaged_ephemeral"])
            == CLASS_ENGAGED_EPHEMERAL
        )

    def test_embodied_beats_engaged_ephemeral(self):
        assert (
            classify_by_label_and_tags(None, ["embodied", "engaged_ephemeral"])
            == CLASS_EMBODIED
        )

    def test_empty_string_label_is_treated_as_no_label(self):
        assert classify_by_label_and_tags("", ["ephemeral"]) == CLASS_EPHEMERAL

    def test_accepts_iterable_tags(self):
        """DB rows can deliver tags as list, tuple, or set — all must work."""
        assert (
            classify_by_label_and_tags(None, ("ephemeral",)) == CLASS_EPHEMERAL
        )
        assert (
            classify_by_label_and_tags(None, {"ephemeral"}) == CLASS_EPHEMERAL
        )

    def test_classify_agent_delegates_to_canonical(self):
        """The adapter must produce the same answer as the canonical fold
        for any input that can be expressed via a meta object."""
        cases = [
            (None, None),
            ("Vigil", []),
            ("random", ["ephemeral"]),
            (None, ["embodied", "persistent"]),
            (None, ["engaged_ephemeral", "ephemeral"]),
            (None, ["persistent", "autonomous"]),
        ]
        for label, tags in cases:
            adapter_result = classify_agent(SimpleNamespace(label=label, tags=tags or []))
            canonical_result = classify_by_label_and_tags(label, tags)
            assert adapter_result == canonical_result, (label, tags)
