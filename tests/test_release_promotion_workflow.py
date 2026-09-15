"""Structural guards for the approval-gated release promotion workflow."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/promote-release.yml"
FRESHNESS_PATH = ROOT / "scripts/ci/release_promotion_freshness.sh"
RELEASE_DOC_PATH = ROOT / "docs/operations/RELEASE_PROCESS.md"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text())
JOBS = WORKFLOW["jobs"]
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def _step(job: str, name: str) -> dict:
    return next(step for step in JOBS[job]["steps"] if step.get("name") == name)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _freshness_repo(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "init", "-b", "master", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release-test@example.com")
    (repo / "PUBLISHED_VERSION").write_text("2.22.0\n")
    (repo / "release.txt").write_text("candidate\n")
    _git(repo, "add", "PUBLISHED_VERSION", "release.txt")
    _git(repo, "commit", "-m", "release candidate")
    source_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "v2.22.1")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "master")
    _git(repo, "push", "origin", "v2.22.1")
    return repo, source_sha


def _run_freshness(
    tmp_path: Path,
    repo: Path,
    source_sha: str,
    *,
    mode: str,
    tag_digest: str = DIGEST_A,
    latest_digest: str = DIGEST_B,
    prior_latest: str = DIGEST_B,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "freshness-bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *":latest"*) digest="$FAKE_LATEST_DIGEST" ;;
  *":$RELEASE_TAG"*) digest="$FAKE_TAG_DIGEST" ;;
  *) echo "unexpected docker invocation: $*" >&2; exit 2 ;;
esac
printf '{"digest":"%s"}\n' "$digest"
"""
    )
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RELEASE_TAG": "v2.22.1",
        "VERSION": "2.22.1",
        "SOURCE_SHA": source_sha,
        "DIGEST": DIGEST_A,
        "PRIOR_LATEST": prior_latest,
        "REGISTRY": "ghcr.io",
        "IMAGE_NAME": "cirwel/unitares",
        "FAKE_TAG_DIGEST": tag_digest,
        "FAKE_LATEST_DIGEST": latest_digest,
    }
    return subprocess.run(
        [str(FRESHNESS_PATH), mode],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


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
    assert '--source-digest "$SOURCE_SHA"' in script
    image = _step("verify", "Resolve the index digest, platforms, and SBOMs")["run"]
    assert '"linux/amd64,linux/arm64"' in image
    assert ".SBOM" in image


def test_manual_publication_keeps_the_workflow_ref_bound_to_the_tag():
    release_doc = RELEASE_DOC_PATH.read_text()
    assert "--ref vX.Y.Z -f ref=vX.Y.Z" in release_doc
    assert "historical tag that predates the workflow cannot satisfy" in release_doc


def test_mutation_jobs_revalidate_before_writing():
    assert JOBS["promote"]["permissions"]["actions"] == "read"
    promote_names = [step.get("name") for step in JOBS["promote"]["steps"]]
    assert promote_names.index("Revalidate release evidence after approval") < (
        promote_names.index("Log in to GHCR")
    )
    assert promote_names.index("Revalidate release evidence after approval") < (
        promote_names.index("Point latest at the verified digest without rebuilding")
    )
    pin_names = [step.get("name") for step in JOBS["pin"]["steps"]]
    assert pin_names.index("Refuse a stale pin re-run") < pin_names.index(
        "Push a branch that advances PUBLISHED_VERSION"
    )

    promote = _step("promote", "Revalidate release evidence after approval")["run"]
    assert "release_promotion_freshness.sh promote" in promote
    assert '.reviewers[]?' in promote and ".reviewer.id" in promote
    assert '.type == "User"' in promote and '.type == "Team"' in promote
    assert '--source-digest "$SOURCE_SHA"' in promote
    pin = _step("pin", "Refuse a stale pin re-run")["run"]
    assert "release_promotion_freshness.sh pin" in pin
    assert "gh release view" in pin
    assert "gh attestation verify" in pin
    assert '--source-digest "$SOURCE_SHA"' in pin
    assert JOBS["pin"]["permissions"]["attestations"] == "read"


def test_freshness_guard_accepts_unchanged_forward_release(tmp_path: Path):
    repo, source_sha = _freshness_repo(tmp_path)
    promote = _run_freshness(tmp_path, repo, source_sha, mode="promote")
    assert promote.returncode == 0, promote.stderr
    pin = _run_freshness(
        tmp_path, repo, source_sha, mode="pin", latest_digest=DIGEST_A
    )
    assert pin.returncode == 0, pin.stderr


def test_freshness_guard_refuses_an_older_release_rerun(tmp_path: Path):
    repo, source_sha = _freshness_repo(tmp_path)
    (repo / "release.txt").write_text("newer\n")
    _git(repo, "add", "release.txt")
    _git(repo, "commit", "-m", "newer release")
    _git(repo, "tag", "v2.23.0")
    result = _run_freshness(tmp_path, repo, source_sha, mode="promote")
    assert result.returncode != 0
    assert "no longer the newest server tag" in result.stderr


def test_freshness_guard_refuses_a_completed_version_rerun(tmp_path: Path):
    repo, source_sha = _freshness_repo(tmp_path)
    (repo / "PUBLISHED_VERSION").write_text("2.22.1\n")
    result = _run_freshness(tmp_path, repo, source_sha, mode="pin")
    assert result.returncode != 0
    assert "not a fresh forward pin" in result.stderr


def test_freshness_guard_refuses_a_moved_source_tag(tmp_path: Path):
    repo, source_sha = _freshness_repo(tmp_path)
    (repo / "release.txt").write_text("moved\n")
    _git(repo, "add", "release.txt")
    _git(repo, "commit", "-m", "move tag target")
    _git(repo, "tag", "-f", "v2.22.1")
    result = _run_freshness(tmp_path, repo, source_sha, mode="promote")
    assert result.returncode != 0
    assert "moved from verified source" in result.stderr


def test_freshness_guard_refuses_a_changed_release_image(tmp_path: Path):
    repo, source_sha = _freshness_repo(tmp_path)
    result = _run_freshness(
        tmp_path, repo, source_sha, mode="promote", tag_digest=DIGEST_C
    )
    assert result.returncode != 0
    assert "not verified digest" in result.stderr


def test_freshness_guard_refuses_latest_drift_during_approval(tmp_path: Path):
    repo, source_sha = _freshness_repo(tmp_path)
    result = _run_freshness(
        tmp_path,
        repo,
        source_sha,
        mode="promote",
        prior_latest=DIGEST_C,
        latest_digest=DIGEST_B,
    )
    assert result.returncode != 0
    assert "latest changed during approval" in result.stderr


def test_freshness_guard_refuses_pin_when_latest_moved(tmp_path: Path):
    repo, source_sha = _freshness_repo(tmp_path)
    result = _run_freshness(tmp_path, repo, source_sha, mode="pin")
    assert result.returncode != 0
    assert "refusing a stale pin" in result.stderr


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


def _run_pin_evidence_check(
    tmp_path: Path,
    *,
    draft: str = "false",
    attestation: str = '[{"verificationResult":{"verified":true}}]',
    attestation_exit_code: int = 0,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "pin-bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-} ${2:-}" in
  "release view")
    printf '%s\n' "$FAKE_RELEASE_DRAFT"
    ;;
  "attestation verify")
    printf '%s\n' "$FAKE_ATTESTATION"
    exit "$FAKE_ATTESTATION_EXIT_CODE"
    ;;
  *)
    echo "unexpected gh invocation: $*" >&2
    exit 2
    ;;
esac
"""
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    script = _step("pin", "Refuse a stale pin re-run")["run"].replace(
        "scripts/ci/release_promotion_freshness.sh pin", "true", 1
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RELEASE_TAG": "v2.22.1",
        "SOURCE_SHA": "a" * 40,
        "DIGEST": DIGEST_A,
        "REGISTRY": "ghcr.io",
        "IMAGE_NAME": "cirwel/unitares",
        "GITHUB_REPOSITORY": "cirwel/unitares",
        "FAKE_RELEASE_DRAFT": draft,
        "FAKE_ATTESTATION": attestation,
        "FAKE_ATTESTATION_EXIT_CODE": str(attestation_exit_code),
    }
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_pin_evidence_recheck_accepts_a_published_attested_release(tmp_path: Path):
    result = _run_pin_evidence_check(tmp_path)
    assert result.returncode == 0, result.stderr


def test_pin_evidence_recheck_refuses_a_draft_release(tmp_path: Path):
    result = _run_pin_evidence_check(tmp_path, draft="true")
    assert result.returncode != 0
    assert "no longer has a published release page" in result.stderr


def test_pin_evidence_recheck_refuses_missing_or_invalid_attestation(tmp_path: Path):
    unavailable = _run_pin_evidence_check(tmp_path, attestation_exit_code=1)
    assert unavailable.returncode != 0

    empty = _run_pin_evidence_check(tmp_path, attestation="[]")
    assert empty.returncode != 0


def _run_gate(
    tmp_path: Path,
    environment_response: str,
    *,
    gh_exit_code: int = 0,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "$FAKE_GH_EXIT_CODE" -ne 0 ]; then
  printf '%s\n' "$FAKE_GH_RESPONSE"
  exit "$FAKE_GH_EXIT_CODE"
fi
jq_filter=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --jq)
      jq_filter=$2
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if [ -z "$jq_filter" ]; then
  echo "fake gh expected --jq" >&2
  exit 2
fi
printf '%s\n' "$FAKE_GH_RESPONSE" | jq -r "$jq_filter"
"""
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    script = _step("verify", "Refuse an ungated promotion environment")["run"]
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "cirwel/unitares",
        "PROMOTION_ENVIRONMENT": "release-promotion",
        "FAKE_GH_RESPONSE": environment_response,
        "FAKE_GH_EXIT_CODE": str(gh_exit_code),
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
        '{"message":"Not Found","status":"404"}',
        gh_exit_code=1,
    )
    assert result.returncode != 0
    assert "must exist with a required reviewer" in result.stderr


def test_environment_without_reviewers_fails_closed(tmp_path: Path):
    response = '{"protection_rules": []}'
    assert _run_gate(tmp_path, response).returncode != 0


def test_required_reviewer_rule_without_people_fails_closed(tmp_path: Path):
    for reviewers in ("[]", "null"):
        response = (
            '{"protection_rules": [{"type":"required_reviewers",'
            f'"reviewers":{reviewers}'
            "]}]}"
        )
        assert _run_gate(tmp_path, response).returncode != 0

    missing_reviewers = '{"protection_rules":[{"type":"required_reviewers"}]}'
    assert _run_gate(tmp_path, missing_reviewers).returncode != 0


def test_malformed_reviewer_entries_fail_closed(tmp_path: Path):
    invalid_reviewers = [
        "null",
        "{}",
        '{"type":"Robot","reviewer":{"id":7}}',
        '{"type":"User","reviewer":null}',
        '{"type":"User","reviewer":{}}',
        '{"type":"Team","reviewer":{"id":0}}',
        '{"type":"Team","reviewer":{"id":"7"}}',
    ]
    for reviewer in invalid_reviewers:
        response = (
            '{"protection_rules":[{"type":"required_reviewers","reviewers":['
            f"{reviewer}"
            "]}]}"
        )
        assert _run_gate(tmp_path, response).returncode != 0, reviewer


def test_environment_with_a_reviewer_passes(tmp_path: Path):
    response = (
        '{"protection_rules": [{"type":"required_reviewers",'
        '"reviewers":[{"type":"User","reviewer":{"id":7,"login":"operator"}}]}]}'
    )
    assert _run_gate(tmp_path, response).returncode == 0
