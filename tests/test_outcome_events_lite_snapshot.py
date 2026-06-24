"""P2 (#604 dogfood 2026-06-24): record_result/outcome_event default to a
lite eisv_snapshot; the full EISV ontology (state_semantics role table +
hierarchy, source-meta, sensor-divergence history) is opt-in.
"""

from src.mcp_handlers.observability.outcome_events import (
    _coerce_bool_flag,
    _lite_eisv_snapshot,
    _LITE_SNAPSHOT_KEYS,
)


def _full_snapshot():
    return {
        "eisv": {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.0},
        "primary_eisv": {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.0},
        "primary_eisv_source": "behavioral",
        "primary_eisv_source_meta": {"name": "behavioral", "desc": "..."},
        "behavioral_eisv": {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.0, "confidence": 0.9},
        "ode_eisv": {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.0},
        "ode_diagnostics": {"phi": 0.5, "verdict": "proceed", "regime": "STABLE"},
        "sensor_divergence": None,
        "sensor_divergence_recent": [{"E": 0.01}] * 20,
        "state_semantics": {
            "primary_eisv_role": "...",
            "behavioral_eisv_role": "...",
            "ode_eisv_role": "...",
            "ode_diagnostics_role": "...",
            "sensor_divergence_role": "...",
            "measurement_policy_contract": "...",
            "hierarchy": ["1.", "2."],
        },
    }


class TestCoerceBoolFlag:
    def test_bool_true(self):
        assert _coerce_bool_flag(True) is True

    def test_string_true_variants(self):
        for v in ("true", "1", "yes", "y", "T", " True "):
            assert _coerce_bool_flag(v) is True

    def test_falsey(self):
        for v in (False, 0, "", "no", "false", None, "0"):
            assert _coerce_bool_flag(v) is False


class TestLiteSnapshot:
    def test_lite_drops_heavy_ontology(self):
        lite = _lite_eisv_snapshot(_full_snapshot())
        # The expensive self-description is gone.
        assert "state_semantics" not in lite
        assert "primary_eisv_source_meta" not in lite
        assert "sensor_divergence_recent" not in lite
        # The actual state numbers and active source survive.
        assert lite["primary_eisv"] == {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.0}
        assert lite["primary_eisv_source"] == "behavioral"
        assert lite["semantics_omitted"] is True
        assert "include_semantics" in lite["hint"]

    def test_lite_keys_are_subset_of_full(self):
        full = _full_snapshot()
        for k in _LITE_SNAPSHOT_KEYS:
            assert k in full

    def test_none_passthrough(self):
        assert _lite_eisv_snapshot(None) is None

    def test_lite_is_much_smaller_than_full(self):
        full = _full_snapshot()
        lite = _lite_eisv_snapshot(full)
        assert len(repr(lite)) < len(repr(full))
