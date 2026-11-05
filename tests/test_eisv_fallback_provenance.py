"""Consumed fallback inputs and offline inherited ancestry, without policy changes."""
from copy import deepcopy
from datetime import datetime
import json
from unittest.mock import patch

import pytest

from scripts.analysis.eisv_provenance_replay import input_ledger, replay
from src.behavioral_sensor import compute_behavioral_sensor_eisv
from src.behavioral_state import BehavioralEISV
from src.dual_log.continuity import ContinuityLayer, compute_continuity_metrics
from src.dual_log.operational import OperationalEntry
from src.dual_log.reflective import create_reflective_entry
from src.eisv_telemetry import (
    build_behavioral_derivation, build_continuity_fallback_derivation,
    build_eisv_telemetry_envelope,
)
from src.governance_monitor import UNITARESMonitor


def metrics(tokens=2000, gap=60000, complexity=0.5):
    op = OperationalEntry(
        timestamp=datetime(2026, 11, 1), agent_id="fallback-fixture",
        response_tokens=tokens, response_chars=tokens * 4, has_code_blocks=False,
        code_block_count=0, list_item_count=0, paragraph_count=1,
        question_count=0, latency_ms=gap,
    )
    return compute_continuity_metrics(op, create_reflective_entry("fallback-fixture", complexity=complexity))


def fallback(cm, excluded=False):
    obs = {
        "E": cm.E_input if cm.E_input is not None else 0.5,
        "I": 0.5 if excluded or cm.I_input is None else cm.I_input,
        "S": 0.2 if excluded or cm.S_input is None else cm.S_input,
    }
    return obs, build_continuity_fallback_derivation(cm, template_divergence_excluded=excluded, computed=obs)


def envelope(state, obs, trace, source="continuity_fallback"):
    state.update(**{f"{d}_obs": obs[d] for d in "EIS"})
    snapshot = state.to_dict_for_persistence()
    snapshot["obs_source"] = source
    return build_eisv_telemetry_envelope(
        metrics=snapshot, behavioral_snapshot=snapshot, submitted_sensor=None,
        submitted_source=None, derivation=trace, policy_evaluation=None, enforcement=None,
    )


@pytest.mark.parametrize("tokens,gap,expected,branch,clipped", [
    (2000, 10000, 1.0, "report_tokens_per_checkin_gap", False),
    (2000, 60000, 0.3, "report_tokens_per_checkin_gap", True),
    (0, 60000, 0.3, "report_tokens_per_checkin_gap", True),
    (0, None, 0.5, "report_length_fallback", False),
    (0, 0, 0.5, "report_length_fallback", False),
    (2000, None, 1.0, "report_length_fallback", True),
])
def test_exact_energy_branch_clipping_and_zero_report(tokens, gap, expected, branch, clipped):
    cm = metrics(tokens, gap)
    trace = cm.input_derivation
    assert trace["inputs"]["response_tokens"] == tokens
    assert trace["inputs"]["checkin_gap_ms"] == gap
    assert cm.E_input == expected == trace["E"]["value"]
    assert trace["E"]["branch"] == branch
    assert trace["E"]["clip_applied"] is clipped
    # A boundary hit is distinct from an actual change by clipping.
    assert trace["E"]["at_bound"] == (expected in (0.3, 1.0))
    assert trace["E"]["branch_reason"] == ("missing_gap" if gap is None else "nonpositive_gap" if gap <= 0 else "positive_gap")


def test_defaults_exclusion_and_unknown_are_distinct():
    cm = metrics(complexity=None)
    _, trace = fallback(cm)
    assert trace["selection"]["I"]["dependency"] == "default_dependent"
    assert trace["selection"]["S"]["default_reason"] is None
    obs, excluded = fallback(cm, True)
    assert obs["I"] == 0.5 and obs["S"] == 0.2
    assert excluded["selection"]["I"]["default_reason"] == "template_divergence_excluded"
    assert excluded["selection"]["I"]["dependency"] == "explicit_default"
    cm.input_derivation = None
    cm.E_input = None
    _, unknown = fallback(cm)
    assert unknown["selection"]["E"]["default_reason"] == "missing_continuity_input"
    assert unknown["selection"]["I"]["dependency"] == "unknown"
    assert unknown["missing_inputs"] == ["continuity_derivation_trace"]


def test_capture_precedes_later_tool_and_roc_changes_and_excludes_text():
    layer = ContinuityLayer("fallback-fixture")
    layer._prev_derived_complexity = 0.9
    cm = layer.process_update(
        "secret-canary report", self_complexity=None,
        tool_usage_stats={"total_calls": 10, "unique_tools": 5},
    )
    original = cm.input_derivation["intermediates"]["complexity_divergence"]
    assert original == 0.2
    assert cm.complexity_divergence != original
    obs, trace = fallback(cm)
    assert obs["I"] == 0.8
    assert trace["selection"]["I"]["trace_matches_input"] is True
    assert "secret-canary" not in json.dumps(cm.to_dict())
    # A custom producer changing the input cannot inherit a stale explanation.
    cm.I_input = 0.1
    assert fallback(cm)[1]["selection"]["I"]["dependency"] == "unknown"


@pytest.mark.parametrize("excluded", [False, True])
def test_monitor_records_consumed_fallback_and_telemetry_failure_leaves_state_unchanged(excluded):
    cm = metrics()
    cm.continuity_degenerate = True
    request = {
        "response_text": "startup", "complexity": 0.5,
        "epistemic_class": "substrate_observation" if excluded else "agent_report",
        "_eisv_derivation": {"kind": "stale_attempt"},
    }
    states = []
    for broken in (False, True):
        monitor = UNITARESMonitor(agent_id="fallback-fixture", load_state=False)
        submitted = deepcopy(request)
        with patch.object(monitor.continuity_layer, "process_update", return_value=cm), patch(
            "src.governance_monitor.compute_behavioral_sensor_eisv", return_value=None,
        ), patch("src.eisv_telemetry.build_continuity_fallback_derivation", wraps=build_continuity_fallback_derivation) as builder:
            if broken:
                builder.side_effect = RuntimeError("telemetry unavailable")
            monitor.process_update(submitted)
        states.append(monitor._behavioral_state.to_dict_for_persistence())
        assert monitor._behavioral_obs_source == "continuity_fallback"
        if broken:
            assert "_eisv_derivation" not in submitted
        else:
            trace = submitted["_eisv_derivation"]
            assert trace["kind"] == "continuity_fallback"
            assert [trace["selection"][d]["value"] for d in "EIS"] == monitor._behavioral_state.obs_history[-1]
            snapshot = dict(states[-1], obs_source="continuity_fallback")
            saved = build_eisv_telemetry_envelope(
                metrics={}, behavioral_snapshot=snapshot, submitted_sensor=None,
                submitted_source=None, derivation=trace, policy_evaluation=None, enforcement=None,
            )
            assert saved["derivation"] == trace
    assert states[0] == states[1]  # Includes baseline statistics and maturity.


def test_unknown_history_survives_new_defaults_on_both_valence_legs():
    state = BehavioralEISV()
    obs, trace = fallback(metrics(), excluded=True)
    records = [envelope(state, obs, trace) for _ in range(6)]
    report = replay({"agent_id": "synthetic", "records": records})
    row4 = report["rows"][3]
    assert row4["status"] == "reconciled"
    assert row4["effective_gains"]["V"] == pytest.approx(0.45)
    # First saved state is an unknown anchor; updates 2,3,4 retain .45*.5*.55.
    carry = 0.45 * 0.5 * 0.55
    assert row4["V_shared_anchor_coefficient"] == pytest.approx(carry)
    assert row4["coefficients"]["V_I_term"]["unknown"] == pytest.approx(carry)
    assert row4["coefficients"]["V_I_term"]["explicit_default"] == pytest.approx(1 - carry)
    assert row4["coefficients"]["V_E_term"]["explicit_default"] == 0
    # Repeated values are distinct accepted updates, not duplicate ingestion.
    assert all(r["status"] == "reconciled" for r in report["rows"][1:])
    legacy = deepcopy(records)
    for record in legacy[:3]:
        record["derivation"] = {"kind": "continuity_fallback", "inputs": {}}
    historical = replay({"agent_id": "synthetic", "records": legacy})["rows"][3]
    assert historical["coefficients"]["V_I_term"]["unknown"] == pytest.approx(0.55)
    assert historical["coefficients"]["V_I_term"]["explicit_default"] == pytest.approx(0.45)


def test_richer_sensor_reconciles_convex_terms_but_does_not_normalize_additive_s():
    inputs = dict(
        decision_history=["proceed"] * 3, coherence_history=[0.5] * 3,
        regime_history=["STABLE"] * 3, E_history=[0.5] * 3, I_history=[0.5] * 3,
        S_history=[0.2] * 3, V_history=[0.0] * 3, tool_error_rate=0.2,
        tool_call_velocity=8.0,
    )
    obs = compute_behavioral_sensor_eisv(**inputs)
    trace = build_behavioral_derivation(**inputs, computed=obs)
    record = envelope(BehavioralEISV(), obs, trace, source="behavioral")
    e, _ = input_ledger(record, "E")
    assert sum(e.values()) == pytest.approx(1)
    assert e["observed_controller"] > 0
    assert e["explicit_default"] > 0
    s, explanation = input_ledger(record, "S")
    assert s["unresolved_formula"] == 1
    assert "no renormalization" in explanation


def test_replay_gaps_resets_mismatch_missing_metadata_and_duplicate_identity():
    state = BehavioralEISV()
    obs, trace = fallback(metrics())
    records = [envelope(state, obs, trace) for _ in range(6)]
    packet = {"agent_id": "synthetic", "records": [records[0], *records[2:]]}
    rows = replay(packet)["rows"]
    assert rows[1]["status"] == "unknown_anchor"  # missing update 2
    assert rows[2]["status"] == "reconciled"
    damaged = deepcopy(records)
    damaged[1]["measurement"]["behavioral"]["smoothed"]["V"] = 0.9
    assert "mismatch" in replay(dict(packet, records=damaged))["rows"][1]["reason"]
    damaged = deepcopy(records)
    damaged[1]["measurement"]["behavioral"]["alphas"] = None
    assert replay(dict(packet, records=damaged))["rows"][2]["status"] == "unknown_anchor"
    assert replay(dict(packet, records=[records[2], records[0]]))["rows"][1]["status"] == "unknown_anchor"
    with pytest.raises(ValueError, match="duplicate ingestion"):
        replay(dict(packet, records=[records[0], records[0]]))
