"""Structural guards for the approval-gated release promotion workflow."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/promote-release.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text())
JOBS = WORKFLOW["jobs"]


def _step(job: str, name: str) -> dict:
    return next(step for step in JOBS[job]["steps"] if step.get("name") == name)


def test_promotion_is_dispatched_by_hand_for_one_tag():
    events = WORKFLOW.get("on", WORKFLOW.get(True))
    assert set(events) == {"workflow_dispatch"}
    assert set(events["workflow_dispatch"]["inputs"]) == {"tag"}
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_latest_moves_only_behind_the_approval_environment():
    assert JOBS["promote"]["environment"] == "release-promotion"
    assert JOBS["promote"]["needs"] == "verify"
    assert set(JOBS["pin"]["needs"]) == {"verify", "promote"}
    assert "environment" not in JOBS["verify"]
    for job in ("verify", "pin"):
        assert JOBS[job]["permissions"].get("packages") != "write"


def test_promotion_copies_the_verified_digest_and_never_rebuilds():
    text = WORKFLOW_PATH.read_text()
    assert "build-push-action" not in text
    script = _step("promote", "Point latest at the verified digest without rebuilding")
    assert script["env"]["DIGEST"] == "${{ needs.verify.outputs.digest }}"
    assert "imagetools create --prefer-index=false" in script["run"]
    assert '@$DIGEST"' in script["run"]
    assert '"$served" != "$DIGEST"' in script["run"]


def test_verification_binds_provenance_to_the_release_tag():
    script = _step("verify", "Verify build provenance from the release tag")["run"]
    assert "gh attestation verify" in script
    assert "--signer-workflow" in script and "publish-container.yml" in script
    assert '--source-ref "refs/tags/$RELEASE_TAG"' in script
    image = _step("verify", "Resolve the index digest, platforms, and SBOMs")["run"]
    assert '"linux/amd64,linux/arm64"' in image
    assert ".SBOM" in image


def test_an_existing_pin_branch_stops_the_run_before_approval():
    """A re-dispatch must not re-run the approval only to fail at git push."""
    verify = _step("verify", "Require the newest release, ahead of PUBLISHED_VERSION")["run"]
    assert 'ls-remote --exit-code --heads origin "publish/$RELEASE_TAG"' in verify
    pin = _step("pin", "Push a branch that advances PUBLISHED_VERSION")["run"]
    assert pin.index("ls-remote --exit-code --heads") < pin.index("git push")


def test_workflow_inputs_never_reach_a_shell_unquoted():
    for job in JOBS.values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")


def _run_gate(tmp_path: Path, fake_gh: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\n" + fake_gh)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    script = _step("verify", "Refuse an ungated promotion environment")["run"]
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "cirwel/unitares",
        "PROMOTION_ENVIRONMENT": "release-promotion",
    }
    # GitHub runs `run:` scripts with bash -e -o pipefail.
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_missing_environment_fails_closed(tmp_path: Path):
    """gh prints the 404 body on stdout; that must not read as a reviewer count."""
    result = _run_gate(
        tmp_path,
        'echo \'{"message":"Not Found","status":"404"}\'\nexit 1\n',
    )
    assert result.returncode != 0
    assert "must exist with a required reviewer" in result.stderr


def test_environment_without_reviewers_fails_closed(tmp_path: Path):
    assert _run_gate(tmp_path, "echo 0\n").returncode != 0


def test_environment_with_a_reviewer_passes(tmp_path: Path):
    assert _run_gate(tmp_path, "echo 1\n").returncode == 0
