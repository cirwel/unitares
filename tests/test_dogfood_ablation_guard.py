from scripts.diagnostics.dogfood_ablation_guard import (
    identity_neutrality_alert,
    inventory_lane_alert,
    matrix_exclusion_alert,
    parse_inventory_counts,
    render_alert_report,
)


def test_identity_neutrality_accepts_unbound_no_session_metrics():
    metrics = {
        "status": "⚪ unbound",
        "agent_id": None,
        "display_name": None,
        "agent_uuid": None,
        "agent_signature": {"uuid": None},
    }

    assert identity_neutrality_alert(metrics) is None


def test_identity_neutrality_alerts_on_laundered_server_identity():
    metrics = {
        "status": "🟡 moderate",
        "agent_id": "Chronicler",
        "display_name": "Chronicler",
        "agent_uuid": "deb879b6-4ff8-4dee-81ce-0683f4563dc5",
        "agent_signature": {"uuid": None},
    }

    assert identity_neutrality_alert(metrics) == "no-session get_governance_metrics is not identity-neutral"


def test_inventory_lane_guard_requires_beam_and_substrate_counts():
    output = """# Outcome Inventory
strict_bad: 0
eprocess_eligible: 1779
eprocess_eligible_beam: 1494
eprocess_eligible_substrate: 285
"""

    counts = parse_inventory_counts(output)

    assert counts["eprocess_eligible"] == 1779
    assert counts["eprocess_eligible_beam"] == 1494
    assert counts["eprocess_eligible_substrate"] == 285
    assert inventory_lane_alert(counts) is None
    assert inventory_lane_alert({"eprocess_eligible": 1779}) == (
        "outcome inventory no longer exposes BEAM/substrate eprocess lanes"
    )


def test_matrix_guard_requires_default_beam_exclusion_header():
    assert matrix_exclusion_alert("Excluded harness lanes: `beam`\n") is None
    assert matrix_exclusion_alert("# EISV Ablation Matrix\n") == (
        "ablation matrix default no longer excludes BEAM harness lane"
    )


def test_render_alert_report_is_silent_when_all_guards_pass():
    assert render_alert_report([], ["inventory=eprocess_eligible=1"]) == ""


def test_render_alert_report_includes_signal_evidence_next_when_alerting():
    report = render_alert_report(
        ["no-session get_governance_metrics is not identity-neutral"],
        ["no_session_metrics={...}"],
    )

    assert report.startswith("UNITARES dogfood/ablation guard\n")
    assert "Signal: no-session get_governance_metrics is not identity-neutral" in report
    assert "- no_session_metrics={...}" in report
    assert "Next: inspect identity proof-origin" in report
