"""Supervised resident validation invocation tests."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.resident_validation import ResidentProfile
from src.resident_validation_invocation import (
    InvocationLockHeld,
    SupervisedInvocationPlan,
    acquire_invocation_lock,
    run_supervised_canary_invocation,
)


def _profile() -> ResidentProfile:
    """Build a low-authority resident profile for invocation tests."""
    return ResidentProfile(
        cohort_id="rv-supervised",
        resident_id="resident-dogfood-1",
        resident_name="Resident Dogfood Canary",
        role="dogfood_probe",
        cadence_seconds=600,
        observation_scope=("repo", "ci"),
    )


def test_invocation_lock_rejects_live_holder_and_allows_expired_lock(
    tmp_path: Path,
) -> None:
    """A supervised invocation lease blocks concurrent live runs but expires."""
    lock_path = tmp_path / "resident.lock.json"
    now = datetime.now(timezone.utc)

    first = acquire_invocation_lock(
        lock_path,
        owner="resident-dogfood-1",
        now=now,
        ttl_seconds=60,
    )

    assert first["owner"] == "resident-dogfood-1"
    with pytest.raises(InvocationLockHeld):
        acquire_invocation_lock(
            lock_path,
            owner="resident-dogfood-2",
            now=now + timedelta(seconds=1),
            ttl_seconds=60,
        )

    refreshed = acquire_invocation_lock(
        lock_path,
        owner="resident-dogfood-2",
        now=now + timedelta(seconds=61),
        ttl_seconds=60,
    )

    assert refreshed["owner"] == "resident-dogfood-2"
    assert json.loads(lock_path.read_text(encoding="utf-8"))["owner"] == (
        "resident-dogfood-2"
    )


def test_invocation_plan_is_local_only_and_rejects_write_authority(
    tmp_path: Path,
) -> None:
    """The supervisor layer may write local state/audit only, not UNITARES/GitHub."""
    with pytest.raises(ValueError, match="local-only"):
        SupervisedInvocationPlan(
            profile=_profile(),
            state_path=tmp_path / "ticks.jsonl",
            lock_path=tmp_path / "resident.lock.json",
            audit_path=tmp_path / "invocations.jsonl",
            allowed_outputs=("local_state_jsonl", "unitares_process_update"),
        )


def test_supervised_invocation_appends_ticks_audit_and_releases_lock(
    tmp_path: Path,
) -> None:
    """A bounded supervised run records local state and audit, then releases lock."""
    plan = SupervisedInvocationPlan(
        profile=_profile(),
        state_path=tmp_path / "ticks.jsonl",
        lock_path=tmp_path / "resident.lock.json",
        audit_path=tmp_path / "invocations.jsonl",
        max_ticks_per_run=2,
    )
    now = datetime.now(timezone.utc)

    result = run_supervised_canary_invocation(
        plan,
        count=2,
        observation="Private observation stays in local state only.",
        prediction="Next supervised tick remains non-mutating.",
        confidence=0.72,
        now=now,
    )

    assert result == {
        "event_type": "resident_validation_supervised_invocation",
        "status": "state_appended",
    }
    assert not plan.lock_path.exists()
    ticks = [
        json.loads(line)
        for line in plan.state_path.read_text(encoding="utf-8").splitlines()
    ]
    audits = [
        json.loads(line)
        for line in plan.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [tick["tick_index"] for tick in ticks] == [1, 2]
    assert audits[-1]["tick_count"] == 2
    assert audits[-1]["authority"] == {
        "allowed_outputs": ["local_state_jsonl", "local_invocation_audit_jsonl"],
        "write_authority": False,
    }

    with pytest.raises(ValueError, match="max_ticks_per_run"):
        run_supervised_canary_invocation(
            plan,
            count=3,
            observation="Still bounded.",
            prediction="Still local-only.",
            confidence=0.7,
            now=now,
        )


def test_supervised_invocation_cli_stdout_is_constant_and_non_sensitive(
    tmp_path: Path,
) -> None:
    """The CLI can be run by cron/launchd without leaking private tick content."""
    state_path = tmp_path / "resident-canary.jsonl"
    lock_path = tmp_path / "resident.lock.json"
    audit_path = tmp_path / "invocations.jsonl"
    script = Path("scripts/diagnostics/resident_validation_supervised_invocation.py")
    private_observation = "Private observation: token-like material stays local."
    private_prediction = "Private prediction: local context should not hit stdout."

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--cohort-id",
            "rv-supervised",
            "--resident-id",
            "resident-dogfood-1",
            "--resident-name",
            "Resident Dogfood Canary",
            "--role",
            "dogfood_probe",
            "--cadence-seconds",
            "600",
            "--observation",
            private_observation,
            "--prediction",
            private_prediction,
            "--confidence",
            "0.72",
            "--state-path",
            str(state_path),
            "--lock-path",
            str(lock_path),
            "--audit-path",
            str(audit_path),
            "--count",
            "2",
            "--max-ticks-per-run",
            "2",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "event_type": "resident_validation_supervised_invocation",
        "status": "state_appended",
    }
    assert private_observation not in completed.stdout
    assert private_prediction not in completed.stdout
    assert "resident-dogfood-1" not in completed.stdout
    assert str(state_path) not in completed.stdout
    assert len(state_path.read_text(encoding="utf-8").splitlines()) == 2
    assert len(audit_path.read_text(encoding="utf-8").splitlines()) == 1
