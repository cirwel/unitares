from scripts.diagnostics.dogfood_ablation_guard import (
    collect_alerts,
    identity_neutrality_alert,
    inventory_lane_alert,
    matrix_exclusion_alert,
    matrix_grouped_lane_alert,
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


def test_grouped_matrix_guard_requires_visible_beam_lane():
    grouped = """# EISV Ablation Matrix
Harness lane mode: grouped

| Lane | Scope | Window days | Lead min | Trusted | Bad | Prior state | Prior risk | Baseline AUC | Baseline Brier | Best EISV/prior model | AUC delta | AUC delta 95% CI | Brier improvement | Brier improvement 95% CI | Brier perm p | Beats both? | Conclusion |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| beam | task | 90 | 30 | 1200 | 80 | 0 | 0 | - | - | - | - | - | - | - | - | no | INCONCLUSIVE |
| substrate | task | 90 | 30 | 400 | 5 | 300 | 300 | 0.9 | 0.01 | prior_risk | 0.0 | - | 0.0 | - | - | no | SKEPTICAL |
"""

    assert matrix_grouped_lane_alert(grouped) is None
    assert matrix_grouped_lane_alert(grouped.replace("| beam |", "| substrate |", 1)) == (
        "ablation matrix grouped mode no longer exposes BEAM harness lane"
    )


def test_collect_alerts_runs_grouped_matrix_for_beam_visibility(monkeypatch, tmp_path):
    calls = []

    def fake_call_tool_no_session(http_url, name, arguments):
        return {"status": "⚪ unbound", "agent_signature": {"uuid": None}}

    def fake_run_repo_script(repo, python, script, args, *, timeout_seconds):
        calls.append((script, tuple(args)))
        if script.endswith("outcome_inventory.py"):
            return "eprocess_eligible: 2\neprocess_eligible_beam: 1\neprocess_eligible_substrate: 1\nstrict_bad: 0\n"
        if "--group-by-harness-lane" in args:
            return "Harness lane mode: grouped\n| Lane | Scope | Beats both? |\n|---|---|---|\n| beam | task | no |\n"
        return "Excluded harness lanes: `beam`\n"

    monkeypatch.setattr(
        "scripts.diagnostics.dogfood_ablation_guard.call_tool_no_session",
        fake_call_tool_no_session,
    )
    monkeypatch.setattr(
        "scripts.diagnostics.dogfood_ablation_guard.run_repo_script",
        fake_run_repo_script,
    )

    alerts, evidence = collect_alerts(
        http_url="http://localhost:8767",
        repo=tmp_path,
        python="python3",
        timeout_seconds=1,
    )

    assert alerts == []
    assert any("--group-by-harness-lane" in args for _, args in calls)
    assert "matrix_grouped_has_beam=True" in evidence


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
