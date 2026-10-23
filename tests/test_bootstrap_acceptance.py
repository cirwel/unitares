"""Safety and failure-propagation checks for the real-Postgres CI harness."""

import hashlib
import importlib.util
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bootstrap_acceptance", ROOT / "scripts/ci/bootstrap_acceptance.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def test_live_database_names_are_refused_before_any_sql(monkeypatch):
    monkeypatch.setattr(acceptance, "query", lambda *args: pytest.fail("must not query a live DB"))
    with pytest.raises(RuntimeError, match="dedicated database"):
        acceptance.empty_database("psql", "unused", "governance")


@pytest.mark.parametrize("responses, message", [
    (["governance"], "does not match"),
    (["unitares_acceptance_fresh", "core"], "empty database"),
    (["unitares_acceptance_fresh", "", "1"], "other clients"),
])
def test_wrong_existing_or_in_use_database_is_refused(monkeypatch, responses, message):
    answers = iter(responses)
    monkeypatch.setattr(acceptance, "query", lambda *args: next(answers))
    with pytest.raises(RuntimeError, match=message):
        acceptance.empty_database("psql", "unused", "unitares_acceptance_fresh")


def test_checksum_verification_distinguishes_missing_history_from_drift(tmp_path):
    migrations = tmp_path / "db/postgres/migrations"
    migrations.mkdir(parents=True)
    rows = []
    for version in (62, 63):
        payload = f"-- migration {version}\n".encode()
        (migrations / f"{version:03d}_fixture.sql").write_bytes(payload)
        rows.append({"version": version, "checksum": hashlib.sha256(payload).hexdigest()})
    acceptance.verify_checksums(tmp_path, rows, False)
    rows[0]["checksum"] = None
    acceptance.verify_checksums(tmp_path, rows, True)
    with pytest.raises(RuntimeError, match="migration 62"):
        acceptance.verify_checksums(tmp_path, rows, False)
    rows[1]["checksum"] = "0" * 64
    with pytest.raises(RuntimeError, match="migration 63"):
        acceptance.verify_checksums(tmp_path, rows, True)


@pytest.mark.parametrize("delegate, migration, expected, marked", [
    ("/usr/bin/true", "062_fixture.sql", 97, True),
    ("/usr/bin/true", "061_fixture.sql", 0, False),
    ("/usr/bin/false", "062_fixture.sql", 1, False),
])
def test_interruption_occurs_only_after_successful_062(delegate, migration, expected, marked, tmp_path):
    # Test the fault injector's exit handling, not SQL semantics. CI supplies
    # real psql and verifies the durable ledger after the injection.
    directory = tmp_path / "bin"
    marker = acceptance.interruption_wrapper(directory, delegate)
    result = subprocess.run([str(directory / "psql"), "-f", migration], capture_output=True)
    assert result.returncode == expected
    assert marker.exists() is marked


def test_native_workflow_runs_both_scenarios_on_candidate_artifact():
    workflow = yaml.safe_load((ROOT / ".github/workflows/native-bootstrap.yml").read_text())
    job = workflow["jobs"]["acceptance"]
    assert "postgres" in job["services"]
    assert "redis" in job["services"]
    checkout = next(step for step in job["steps"] if step.get("name") == "Check out the exact candidate source")
    assert checkout["with"]["ref"] == "${{ inputs.source-ref || github.sha }}"
    run = next(step["run"] for step in job["steps"] if step.get("name") == "Exercise fresh installation and interrupted recovery")
    assert "set -euo pipefail" in run
    assert "for scenario in fresh interrupted" in run
    assert '--app-root "$RUNNER_TEMP/native-artifact"' in run
    assert "|| true" not in run
    assert not job.get("continue-on-error")
    assert not any(step.get("continue-on-error") for step in job["steps"])


def test_failed_subprocess_is_not_reported_as_success(tmp_path):
    result = acceptance.run_logged(["/usr/bin/false"], tmp_path, {}, tmp_path / "failure.log")
    assert result == 1
