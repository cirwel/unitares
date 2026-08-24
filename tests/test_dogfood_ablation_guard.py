import json

from scripts.diagnostics.dogfood_ablation_guard import (
    collect_alerts,
    dogfood_response_alerts,
    extract_dogfood_response,
    identity_neutrality_alert,
    inventory_lane_alert,
    parse_inventory_counts,
    render_alert_report,
    risk_authority_ablation_alert,
)


def test_dogfood_response_guard_flags_observed_contract_violations():
    output = """injected prompt text containing [SILENT]
## Response
**Selected surfaces tested this pulse:**
- Surface 9
- Surface 1

No actionable friction found. The system is healthy.
**KG Note Created:** `/Users/example/verify_delivery.py`
[SILENT]
"""

    assert extract_dogfood_response(output).startswith("**Selected surfaces")
    assert dogfood_response_alerts(output) == [
        "dogfood response combines [SILENT] with substantive content",
        "dogfood response declares multiple primary surfaces",
        "dogfood all-clear omits the required open-findings query",
        "dogfood response labels a local file as a KG note",
    ]


def test_dogfood_response_guard_accepts_one_surface_and_named_query():
    output = """## Response
**Selected Surface:** #4

Open findings query: `knowledge(action="search", query="dogfood friction", status="open")`.
No actionable friction found after the query and edge-case probe.
"""

    assert dogfood_response_alerts(output) == []


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


def test_risk_authority_guard_requires_three_bounded_passing_arms():
    report = {
        "schema": "risk_authority_ablation.v1",
        "mode": "synthetic_restart_contract",
        "live_outcomes_read": False,
        "live_governance_mutated": False,
        "passed": True,
        "arms": [{"passed": True}, {"passed": True}, {"passed": True}],
    }

    assert risk_authority_ablation_alert(report) is None
    assert risk_authority_ablation_alert({**report, "live_outcomes_read": True}) == (
        "risk-authority ablation contract failed"
    )
    assert risk_authority_ablation_alert({**report, "arms": report["arms"][:2]}) == (
        "risk-authority ablation contract failed"
    )
    assert risk_authority_ablation_alert({**report, "passed": False}) == (
        "risk-authority ablation contract failed"
    )


def test_collect_alerts_checks_lane_contract_without_reading_live_matrix(monkeypatch, tmp_path):
    calls = []
    dogfood_output_dir = tmp_path / "dogfood-output"
    dogfood_output_dir.mkdir()
    (dogfood_output_dir / "2026-08-20_00-00-00.md").write_text(
        """## Response
**Selected Surface:** #4
Open findings query: `knowledge(action="search", query="dogfood friction")`.
No actionable friction found after the query and edge-case probe.
""",
        encoding="utf-8",
    )

    def fake_call_tool_no_session(http_url, name, arguments):
        return {"status": "⚪ unbound", "agent_signature": {"uuid": None}}

    def fake_run_repo_script(repo, python, script, args, *, timeout_seconds):
        calls.append((script, tuple(args)))
        if script.endswith("outcome_inventory.py"):
            return "eprocess_eligible: 2\neprocess_eligible_beam: 1\neprocess_eligible_substrate: 1\nstrict_bad: 0\n"
        if script.endswith("risk_authority_ablation.py"):
            return json.dumps({
                "schema": "risk_authority_ablation.v1",
                "mode": "synthetic_restart_contract",
                "live_outcomes_read": False,
                "live_governance_mutated": False,
                "passed": True,
                "arms": [
                    {"passed": True},
                    {"passed": True},
                    {"passed": True},
                ],
            })
        assert script == "-m"
        assert args[0] == "pytest"
        return "4 passed\n"

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
        dogfood_output_dir=dogfood_output_dir,
    )

    assert alerts == []
    assert not any(script.endswith("eisv_ablation_matrix.py") for script, _ in calls)
    assert any(script == "-m" and args[0] == "pytest" for script, args in calls)
    assert any(script.endswith("risk_authority_ablation.py") for script, _ in calls)
    assert "ablation_lane_contract_tests=passed; live_outcomes_read=false" in evidence
    assert (
        "risk_authority_ablation=passed; arms=3; live_outcomes_read=false; "
        "live_governance_mutated=false"
    ) in evidence


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
