"""Tests for dogfood friction finding envelopes.

Fresh dogfood runs should surface newcomer-friction as evidence, not as direct
actuation. These tests pin the first CI/CD primitive: normalize a finding,
compute stable dedup tokens, and emit route hints for issue/KG/dialectic/gate
layers without performing those writes directly.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


BASE_FRICTION = {
    "surface": "unitares_mcp",
    "fresh_agent_context": "fresh Hermes dogfood episode; no proof-bearing continuity token",
    "attempted_action": "call process_agent_update with documented mirror payload",
    "expected": "mirror check-in succeeds",
    "observed": "schema rejected initial_state.task as an extra field",
    "evidence_uri": "artifact://run-a/log.txt",
    "repro_command": "hermes mcp call unitares process_agent_update @payload.json",
    "workaround_used": "removed unsupported nested field",
    "severity": "medium",
    "reproducible": True,
    "recurrence_count": 1,
}


def test_build_event_computes_stable_fingerprint_but_condition_token_tracks_evidence():
    from agents.common.dogfood_friction import build_dogfood_friction_event

    first = build_dogfood_friction_event(BASE_FRICTION)
    rerun = build_dogfood_friction_event({**BASE_FRICTION, "evidence_uri": "artifact://run-b/log.txt"})

    assert first["event_type"] == "dogfood_friction_finding"
    assert first["fingerprint"] == rerun["fingerprint"]
    assert first["change_token"] != rerun["change_token"]
    assert len(first["fingerprint"]) == 16
    assert first["severity"] == "medium"
    assert first["agent_id"] == "dogfood-friction"
    assert first["extra"]["kind"] == "dogfood_friction"
    assert first["extra"]["routes"] == ["issue_surface"]
    assert "actuator" in first["extra"]["boundary_note"]


def test_routes_recurring_ambiguous_high_severity_to_separate_layers():
    from agents.common.dogfood_friction import build_dogfood_friction_event

    event = build_dogfood_friction_event({
        **BASE_FRICTION,
        "severity": "high",
        "recurrence_count": 3,
        "ambiguous": True,
        "proposed_action": "block",
    })

    assert event["severity"] == "high"
    assert event["extra"]["routes"] == [
        "ci_gate",
        "issue_surface",
        "kg_note",
        "dialectic_request",
    ]
    assert event["extra"]["proposed_action"] == "block"


@pytest.mark.parametrize("missing", ["surface", "attempted_action", "expected", "observed"])
def test_missing_required_fields_raise_clear_validation_error(missing):
    from agents.common.dogfood_friction import DogfoodFrictionValidationError, build_dogfood_friction_event

    payload = dict(BASE_FRICTION)
    payload.pop(missing)

    with pytest.raises(DogfoodFrictionValidationError, match=missing):
        build_dogfood_friction_event(payload)


def test_script_dry_run_prints_event_payload_without_posting(monkeypatch, capsys):
    module_path = Path(__file__).resolve().parents[3] / "scripts" / "diagnostics" / "dogfood_friction_finding.py"
    spec = importlib.util.spec_from_file_location("dogfood_friction_finding_cli", module_path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    posted = []
    monkeypatch.setattr(cli, "post_dogfood_friction", lambda *args, **kwargs: posted.append((args, kwargs)))

    input_payload = json.dumps(BASE_FRICTION)
    rc = cli.main(["--input-json", input_payload])

    assert rc == 0
    assert posted == []
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["event_type"] == "dogfood_friction_finding"
    assert rendered["extra"]["surface"] == "unitares_mcp"


def test_script_post_mode_calls_post_helper(monkeypatch, capsys):
    module_path = Path(__file__).resolve().parents[3] / "scripts" / "diagnostics" / "dogfood_friction_finding.py"
    spec = importlib.util.spec_from_file_location("dogfood_friction_finding_cli_post", module_path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    calls = []

    def fake_post(payload, *, agent_id, agent_name):
        calls.append({"payload": payload, "agent_id": agent_id, "agent_name": agent_name})
        return True

    monkeypatch.setattr(cli, "post_dogfood_friction", fake_post)

    rc = cli.main([
        "--input-json",
        json.dumps(BASE_FRICTION),
        "--post",
        "--agent-id",
        "ci-dogfood",
        "--agent-name",
        "CI Dogfood",
    ])

    assert rc == 0
    assert calls == [{"payload": BASE_FRICTION, "agent_id": "ci-dogfood", "agent_name": "CI Dogfood"}]
    assert json.loads(capsys.readouterr().out) == {"posted": True}
