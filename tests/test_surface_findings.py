"""Tests for the CI issue-surfacing bridge (scripts/ci/surface_findings.py).

The bridge runs deterministic collectors in CI and emits a normalized,
fingerprinted findings feed that a workflow turns into deduped GitHub issues.
These tests pin the parts that the workflow's dedup contract depends on:

  - fingerprints are stable and byte-identical to the canonical
    agents.common.findings.compute_fingerprint (the workflow keys issue dedup
    on this id; drift would silently re-open every issue),
  - severity mapping is what the issue labels claim,
  - the same finding seen by two collectors is one report entry, not two,
  - ruff/doctor collector output is parsed into the normalized shape.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def mod():
    module_path = PROJECT_ROOT / "scripts" / "ci" / "surface_findings.py"
    spec = importlib.util.spec_from_file_location("surface_findings", module_path)
    assert spec and spec.loader, f"could not load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["surface_findings"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fingerprint identity — the dedup contract the workflow depends on
# ---------------------------------------------------------------------------


def test_fingerprint_matches_canonical(mod):
    """The inlined fingerprint MUST equal agents.common.findings.compute_fingerprint
    for the same parts — otherwise a finding surfaced from CI would not share
    dedup identity with the rest of UNITARES."""
    from agents.common.findings import compute_fingerprint as canonical

    parts = ["ruff", "F401", "src/foo.py", 12]
    assert mod.compute_fingerprint(parts) == canonical(parts)


def test_fingerprint_is_stable_across_instances(mod):
    a = mod.Finding(source="ruff", severity="medium", title="t", message="m",
                    file="src/foo.py", line=12, rule="F401")
    b = mod.Finding(source="ruff", severity="medium", title="t2", message="m2",
                    file="src/foo.py", line=12, rule="F401")
    # title/message differ but identity (source|rule|file|line) is the same.
    assert a.fingerprint == b.fingerprint


def test_fingerprint_distinguishes_line(mod):
    a = mod.Finding(source="ruff", severity="medium", title="t", message="m",
                    file="src/foo.py", line=12, rule="F401")
    b = mod.Finding(source="ruff", severity="medium", title="t", message="m",
                    file="src/foo.py", line=13, rule="F401")
    assert a.fingerprint != b.fingerprint


# ---------------------------------------------------------------------------
# Severity mapping — issue labels claim this
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code,expected", [
    ("F821", "high"),     # undefined name — genuine bug
    ("F401", "medium"),   # unused import
    ("B008", "medium"),   # bugbear
    ("E501", "low"),      # line length
    ("W291", "low"),      # whitespace
    ("ZZZ9", "medium"),   # unknown family -> safe default
])
def test_ruff_severity_mapping(mod, code, expected):
    assert mod._ruff_severity(code) == expected


def test_invalid_severity_coerced_to_medium(mod):
    f = mod.Finding(source="x", severity="bogus", title="t", message="m")
    assert f.severity == "medium"


# ---------------------------------------------------------------------------
# Orchestration — cross-collector dedup + watcher gating
# ---------------------------------------------------------------------------


def test_run_dedups_identical_fingerprints_across_collectors(mod, monkeypatch):
    dup = mod.Finding(source="ruff", severity="high", title="t", message="m",
                      file="src/a.py", line=1, rule="F821")
    # Same identity tuple surfaced by a second collector.
    same = mod.Finding(source="ruff", severity="high", title="other", message="m2",
                       file="src/a.py", line=1, rule="F821")
    assert dup.fingerprint == same.fingerprint

    monkeypatch.setitem(mod.COLLECTORS, "alpha", lambda paths: ([dup], "1"))
    monkeypatch.setitem(mod.COLLECTORS, "beta", lambda paths: ([same], "1"))

    report = mod.run(["alpha", "beta"], paths=[], enable_watcher=False)
    assert len(report.findings) == 1


def test_watcher_disabled_by_default(mod):
    report = mod.run(["watcher"], paths=[], enable_watcher=False)
    assert report.findings == []
    assert "disabled" in report.collectors["watcher"]


def test_unknown_collector_noted_not_crashed(mod):
    report = mod.run(["nope"], paths=[], enable_watcher=False)
    assert "unknown collector" in report.collectors["nope"]


# ---------------------------------------------------------------------------
# Collector parsing — ruff/doctor JSON -> normalized findings
# ---------------------------------------------------------------------------


def test_collect_ruff_parses_diagnostics(mod, monkeypatch):
    fake_json = json.dumps([
        {"code": "F821", "message": "undefined name 'x'",
         "filename": "src/foo.py", "location": {"row": 9, "column": 1}},
    ])

    class FakeProc:
        stdout = fake_json
        stderr = ""
        returncode = 1

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: FakeProc())
    findings, note = mod.collect_ruff(["src"])
    assert len(findings) == 1
    f = findings[0]
    assert f.source == "ruff" and f.rule == "F821" and f.severity == "high"
    assert f.file == "src/foo.py" and f.line == 9
    assert "1 diagnostics" in note


def test_collect_ruff_clean_tree(mod, monkeypatch):
    class FakeProc:
        stdout = ""
        stderr = ""
        returncode = 0

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: FakeProc())
    findings, note = mod.collect_ruff(["."])
    assert findings == []
    assert "0 diagnostics" in note


def test_collect_ruff_missing_binary_skips(mod, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("ruff")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    findings, note = mod.collect_ruff(["."])
    assert findings == []
    assert "not installed" in note


def test_collect_doctor_maps_fail_and_warn(mod, monkeypatch):
    payload = json.dumps({"results": [
        {"name": "schema", "mode": "local", "status": "fail",
         "message": "no db", "detail": "conn refused"},
        {"name": "anchor", "mode": "local", "status": "warn",
         "message": "missing", "detail": ""},
        {"name": "python", "mode": "local", "status": "pass",
         "message": "ok", "detail": ""},
    ]})

    class FakeProc:
        stdout = payload
        stderr = ""
        returncode = 1

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: FakeProc())
    findings, note = mod.collect_doctor([])
    # pass is dropped; fail -> high, warn -> medium.
    assert {f.rule: f.severity for f in findings} == {"schema": "high", "anchor": "medium"}
    assert "conn refused" in next(f.message for f in findings if f.rule == "schema")


# ---------------------------------------------------------------------------
# Report shape — what the workflow consumes
# ---------------------------------------------------------------------------


def test_report_json_sorts_and_counts(mod):
    report = mod.Report(generated_at="now")
    report.findings = [
        mod.Finding(source="ruff", severity="low", title="lo", message="", rule="E1"),
        mod.Finding(source="ruff", severity="critical", title="crit", message="", rule="X1"),
        mod.Finding(source="ruff", severity="medium", title="med", message="", rule="F4"),
    ]
    payload = report.to_json()
    # critical sorts first.
    assert payload["findings"][0]["severity"] == "critical"
    assert payload["summary"]["total"] == 3
    assert payload["summary"]["by_severity"] == {
        "critical": 1, "high": 0, "medium": 1, "low": 1,
    }


def test_main_writes_output_and_exit_code(mod, tmp_path, monkeypatch):
    out = tmp_path / "findings.json"
    # ruff clean -> no findings -> fail-on never trips.
    monkeypatch.setitem(
        mod.COLLECTORS, "ruff", lambda paths: ([], "0 diagnostics"))
    rc = mod.main(["--collectors", "ruff", "--output", str(out), "--fail-on", "low"])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["summary"]["total"] == 0


def test_fail_on_trips_for_high(mod, monkeypatch):
    hit = mod.Finding(source="ruff", severity="high", title="t", message="m", rule="F821")
    monkeypatch.setitem(mod.COLLECTORS, "ruff", lambda paths: ([hit], "1"))
    rc = mod.main(["--collectors", "ruff", "--fail-on", "high"])
    assert rc == 1
