"""Tests for grounding scale constants — provenance invariants.

Spec §3.4 requires every scale constant to carry measurement metadata.
These tests enforce that requirement at import time.
"""
import math

import pytest

from config.governance_config import (
    ScaleConstant,
    S_SCALE,
    I_SCALE,
    E_SCALE,
    DELTA_NORM_MAX,
    ALL_SCALE_CONSTANTS,
)


def test_scale_constant_has_required_fields():
    sc = S_SCALE
    assert sc.value > 0
    assert sc.measured_on
    assert sc.corpus_size >= 0
    assert sc.percentile in {50, 75, 90, 95, 99, None}
    assert sc.provenance in {"measured", "placeholder", "derived", "alias"}


def test_scale_constant_rejects_unknown_provenance():
    with pytest.raises(ValueError, match="unknown provenance"):
        ScaleConstant(
            name="BOGUS", value=1.0, measured_on="2026-04-20",
            corpus_size=0, percentile=None, provenance="guess",
        )


def test_scale_constant_accepts_alias_provenance():
    sc = ScaleConstant(
        name="DELTA_NORM_MAX[TestAlias]", value=0.2018, measured_on="2026-04-20",
        corpus_size=0, percentile=None, provenance="alias",
        notes="Alias to default for testing.",
    )
    assert sc.provenance == "alias"


def test_all_constants_registered_in_manifest():
    assert S_SCALE in ALL_SCALE_CONSTANTS
    assert I_SCALE in ALL_SCALE_CONSTANTS
    assert E_SCALE in ALL_SCALE_CONSTANTS
    assert DELTA_NORM_MAX in ALL_SCALE_CONSTANTS


def test_scale_constants_are_finite_floats():
    for sc in ALL_SCALE_CONSTANTS:
        assert isinstance(sc.value, float)
        assert math.isfinite(sc.value)
        assert sc.value > 0


def test_fleet_defaults_remain_placeholder():
    """Fleet-wide ALL_SCALE_CONSTANTS are placeholders (fallback only).

    Class-conditional constants in DELTA_NORM_MAX_BY_CLASS carry the
    measured values; fleet defaults exist only as a fallback for
    unclassified agents.
    """
    placeholders = [sc for sc in ALL_SCALE_CONSTANTS if sc.provenance == "placeholder"]
    assert len(placeholders) == len(ALL_SCALE_CONSTANTS)


def test_class_conditional_delta_norm_max_is_measured_or_alias():
    """Phase 2 measurement populated DELTA_NORM_MAX_BY_CLASS with measured
    values. 'alias' entries are permitted only when they mirror DEFAULT —
    they exist so residents with no corpus yet don't silently fall back.
    """
    from config.governance_config import (
        DELTA_NORM_MAX_BY_CLASS, DELTA_NORM_MAX_DEFAULT,
    )
    assert len(DELTA_NORM_MAX_BY_CLASS) >= 5  # Lumen, default, Sentinel, Vigil, Watcher
    for cls_name, sc in DELTA_NORM_MAX_BY_CLASS.items():
        assert sc.provenance in {"measured", "alias"}, (
            f"class-conditional {cls_name} should be measured or alias, got {sc.provenance}"
        )
        if sc.provenance == "measured":
            assert sc.corpus_size > 0
            assert sc.percentile == 95
        else:  # alias
            # Alias = no usable corpus: either zero rows, or a sub-threshold
            # count (<30) too thin to measure (e.g. Chronicler N=26 on 06-27).
            assert sc.corpus_size < 30, (
                f"alias entry {cls_name} must declare a sub-threshold corpus_size"
            )
            # Alias must mirror another class's value exactly — not a free guess.
            # Acceptable targets: the fleet placeholder DEFAULT, or any measured
            # class in the same dict.
            peers = {s.value for k, s in DELTA_NORM_MAX_BY_CLASS.items() if k != cls_name}
            allowed = peers | {DELTA_NORM_MAX_DEFAULT.value}
            assert sc.value in allowed, (
                f"alias entry {cls_name} value {sc.value} does not mirror any "
                f"peer or DEFAULT — aliases must not introduce new numeric values"
            )


def test_public_dicts_are_user_agnostic_generic_classes_only():
    """The shipped class-conditional maps must NOT name specific residents — the
    repo is user-agnostic. Only generic behavior classes from the tag taxonomy
    (+ 'default') ship here; named residents come from the deployment-local
    UNITARES_CLASS_CALIBRATION overlay (see the overlay test below).
    """
    from config.governance_config import (
        DELTA_NORM_MAX_BY_CLASS, HEALTHY_OPERATING_POINT_BY_CLASS,
    )
    allowed = {"embodied", "resident_persistent", "engaged_ephemeral",
               "ephemeral", "default"}
    assert set(DELTA_NORM_MAX_BY_CLASS) <= allowed, (
        f"non-generic keys leaked into DELTA_NORM_MAX_BY_CLASS: "
        f"{set(DELTA_NORM_MAX_BY_CLASS) - allowed}")
    assert set(HEALTHY_OPERATING_POINT_BY_CLASS) <= allowed, (
        f"non-generic keys leaked into HEALTHY_OPERATING_POINT_BY_CLASS: "
        f"{set(HEALTHY_OPERATING_POINT_BY_CLASS) - allowed}")


def test_class_calibration_overlay_merges_named_residents(tmp_path, monkeypatch):
    """A deployment supplies named-resident anchors via UNITARES_CLASS_CALIBRATION
    (out of the repo); the loader merges them over the generic defaults."""
    import json, importlib
    overlay = tmp_path / "cc.json"
    overlay.write_text(json.dumps({
        "healthy_operating_point": {"Lumen": [0.316, 0.782, 0.210]},
        "delta_norm_max": {"Lumen": 0.1635},
        "void_threshold": {"Lumen": 0.30},
    }))
    monkeypatch.setenv("UNITARES_CLASS_CALIBRATION", str(overlay))
    import config.governance_config as g
    importlib.reload(g)
    try:
        assert g.get_healthy_operating_point("Lumen") == pytest.approx((0.316, 0.782, 0.210))
        dnm = g.get_delta_norm_max("Lumen")
        assert dnm.value == pytest.approx(0.1635) and dnm.provenance == "overlay"
        assert g.GovernanceConfig.VOID_THRESHOLD_BY_CLASS.get("Lumen") == 0.30
    finally:
        monkeypatch.delenv("UNITARES_CLASS_CALIBRATION", raising=False)
        importlib.reload(g)


def test_class_conditional_lookup_falls_back_for_unknown_classes():
    """Unknown class falls back to fleet-wide DELTA_NORM_MAX_DEFAULT."""
    from config.governance_config import (
        get_delta_norm_max, DELTA_NORM_MAX_DEFAULT,
    )
    assert get_delta_norm_max("nonexistent_class") is DELTA_NORM_MAX_DEFAULT
    assert get_delta_norm_max("embodied").provenance == "measured"


def test_healthy_operating_point_class_conditional():
    """Per-class healthy points exist for measured classes; default for others."""
    from config.governance_config import (
        get_healthy_operating_point, HEALTHY_OPERATING_POINT_DEFAULT,
    )
    embodied_hop = get_healthy_operating_point("embodied")
    assert embodied_hop != HEALTHY_OPERATING_POINT_DEFAULT
    assert all(0.0 <= v <= 1.0 for v in embodied_hop)

    unknown_hop = get_healthy_operating_point("nonexistent")
    assert unknown_hop == HEALTHY_OPERATING_POINT_DEFAULT


def test_engaged_ephemeral_review_cookie_recalibrated():
    """engaged_ephemeral carries measured values, refreshed on the 2026-06-27
    fleet-wide recalibration (was the 2026-05-30 S8a snapshot)."""
    from config.governance_config import (
        DELTA_NORM_MAX_BY_CLASS,
        HEALTHY_OPERATING_POINT_BY_CLASS,
    )

    sc = DELTA_NORM_MAX_BY_CLASS["engaged_ephemeral"]
    assert sc.provenance == "measured"
    assert sc.measured_on == "2026-06-27"
    assert sc.corpus_size == 2115
    assert sc.percentile == 95
    assert sc.value == pytest.approx(0.2952)
    assert HEALTHY_OPERATING_POINT_BY_CLASS["engaged_ephemeral"] == pytest.approx(
        (0.7685, 0.6918, 0.3536)
    )


def test_delta_norm_max_default_covers_full_state_space_diagonal():
    """Fleet-wide default must allow full diagonal so unclassified agents can hit C=0."""
    assert DELTA_NORM_MAX.value >= math.sqrt(3) - 0.01


class TestPopulationScope:
    """How far a calibration constant's evidence actually reaches.

    corpus_size counts OBSERVATIONS. A class fitted on 12,501 turns from one
    device is a prior about that device wearing the class's name, and nothing
    downstream could tell that apart from a broad fit. Under the federation
    commitment — per-principal governance, no shared administrative root — such
    a constant shipped as a class-wide norm IS a shared administrative root for
    what counts as healthy, which is the failure the commitment exists to
    avoid. So breadth has to be recorded, and quotable range derived from it.
    """

    def _c(self, **kw):
        base = dict(
            name="T", value=1.0, measured_on="2026-08-19", corpus_size=100,
            percentile=95, provenance="measured",
        )
        base.update(kw)
        return ScaleConstant(**base)

    def test_unrecorded_is_not_a_claim_of_breadth(self):
        c = self._c()
        assert c.distinct_agents is None
        assert c.population_scope == "unrecorded"
        # Crucially NOT exportable: absence of evidence is not evidence of breadth.
        assert c.federation_exportable is False

    def test_one_agent_is_a_prior_not_a_population(self):
        c = self._c(distinct_agents=1)
        assert c.population_scope == "single_agent"
        assert c.federation_exportable is False

    def test_zero_agents_is_also_single_agent_scope(self):
        assert self._c(distinct_agents=0).population_scope == "single_agent"

    def test_several_agents_one_deployment_is_local_only(self):
        c = self._c(distinct_agents=4)
        assert c.population_scope == "single_principal"
        # Usable locally...
        assert c.federation_exportable is True

    def test_multiple_principals_is_federated(self):
        c = self._c(distinct_agents=9, distinct_principals=3)
        assert c.population_scope == "federated"
        assert c.federation_exportable is True

    def test_principal_count_dominates(self):
        # More than one principal means the fit crossed a trust boundary, which
        # is the property federation cares about, whatever the agent count says.
        assert self._c(distinct_agents=1, distinct_principals=2).population_scope == "federated"

    def test_negative_counts_rejected(self):
        with pytest.raises(ValueError):
            self._c(distinct_agents=-1)
        with pytest.raises(ValueError):
            self._c(distinct_principals=0)

    def test_embodied_is_declared_single_agent(self):
        """REGRESSION. The shipped embodied constants come from one device.

        If a future regen widens the class, this assertion should be UPDATED to
        the new count — not deleted. Deleting it removes the disclosure.
        """
        from config.governance_config import DELTA_NORM_MAX_BY_CLASS

        embodied = DELTA_NORM_MAX_BY_CLASS["embodied"]
        assert embodied.distinct_agents == 1
        assert embodied.population_scope == "single_agent"
        assert embodied.federation_exportable is False
        assert "SINGLE AGENT" in embodied.notes

    def test_no_measured_constant_silently_claims_breadth(self):
        """A measured constant must either declare its breadth or read as unrecorded.

        The failure being blocked is a constant that LOOKS broad because
        corpus_size is large. Either it says how many agents, or its scope says
        it does not know — never a large N standing in for breadth.
        """
        from config.governance_config import DELTA_NORM_MAX_BY_CLASS

        for name, sc in DELTA_NORM_MAX_BY_CLASS.items():
            if sc.provenance != "measured":
                continue
            assert sc.population_scope in {
                "unrecorded", "single_agent", "single_principal", "federated"
            }
            if sc.distinct_agents is None:
                assert sc.federation_exportable is False, (
                    f"{name}: breadth unrecorded but treated as exportable"
                )
