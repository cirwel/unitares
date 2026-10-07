"""Prevent release publication/backfill from moving the default install image."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/publish-container.yml").read_text())
PUBLISH = WORKFLOW["jobs"]["publish"]


def _step(step_id: str) -> dict:
    return next(step for step in PUBLISH["steps"] if step.get("id") == step_id)


def test_publication_only_tags_the_versioned_artifact():
    # PyYAML's YAML 1.1 parser reads the GitHub Actions `on` key as True.
    events = WORKFLOW.get("on", WORKFLOW.get(True))
    assert events["release"]["types"] == ["published"]
    assert set(events["workflow_dispatch"]["inputs"]) == {"ref"}
    metadata = _step("meta")
    assert metadata["with"]["flavor"] == "latest=false"
    assert metadata["with"]["tags"].splitlines() == [
        "type=raw,value=${{ steps.release.outputs.tag }}"
    ]
    push = _step("push")
    assert push["with"]["tags"] == "${{ steps.meta.outputs.tags }}"
    assert push["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert push["with"]["sbom"] is True
    assert push["with"]["provenance"] == "mode=max"


def test_one_release_publishes_every_image_compose_pulls():
    """docker-compose.yml pins the server, lease-plane and database images to the release tag."""
    strategy = PUBLISH["strategy"]
    # One image's failure must not cancel another's build.
    assert strategy["fail-fast"] is False
    artifacts = {entry["artifact"]: entry for entry in strategy["matrix"]["include"]}
    assert set(artifacts) == {"server", "lease-plane", "postgres"}
    assert artifacts["server"]["image_suffix"] == ""
    assert artifacts["server"]["context"] == "."
    assert artifacts["server"]["dockerfile"] == "Dockerfile"
    assert artifacts["lease-plane"]["image_suffix"] == "-lease-plane"
    assert artifacts["lease-plane"]["context"] == "."
    assert artifacts["lease-plane"]["dockerfile"] == "elixir/lease_plane/Dockerfile"
    # The database image builds from db/postgres, matching docker-compose.yml.
    assert artifacts["postgres"]["image_suffix"] == "-postgres"
    assert artifacts["postgres"]["context"] == "db/postgres"
    assert artifacts["postgres"]["dockerfile"] == "db/postgres/Dockerfile.age-vector"
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    assert compose["postgres-age"]["build"]["context"] == "db/postgres"
    assert compose["postgres-age"]["build"]["dockerfile"] == "Dockerfile.age-vector"

    image = "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}${{ matrix.image_suffix }}"
    assert _step("meta")["with"]["images"] == image
    push = _step("push")
    assert push["with"]["context"] == "${{ matrix.context }}"
    assert push["with"]["file"] == "${{ matrix.dockerfile }}"
    # Each image keeps its own build cache instead of evicting the other's.
    assert "scope=${{ matrix.artifact }}" in push["with"]["cache-from"]
    assert "scope=${{ matrix.artifact }}" in push["with"]["cache-to"]
    # The lease plane carries no .git; its /health build_sha comes from here.
    assert "UNITARES_BUILD_SHA=" in push["with"]["build-args"]
    attest = next(
        step
        for step in PUBLISH["steps"]
        if "attest-build-provenance" in step.get("uses", "")
    )
    assert attest["with"]["subject-name"] == image
    assert attest["with"]["subject-digest"] == "${{ steps.push.outputs.digest }}"
    assert attest["with"]["push-to-registry"] is True


def test_a_published_release_tag_is_never_replaced():
    """Compose pulls every image by tag; a re-dispatch must not rebuild
    over the digest Promote Release verified."""
    guard = _step("existing")
    # Every artifact, not only the lease plane: Compose now pulls all three.
    assert "if" not in guard
    assert "docker buildx imagetools inspect" in guard["run"]
    assert 'skip=true' in guard["run"]
    steps = PUBLISH["steps"]
    login = next(i for i, st in enumerate(steps) if "login-action" in st.get("uses", ""))
    assert login < steps.index(guard) < steps.index(_step("push"))
    assert _step("push")["if"] == "steps.existing.outputs.skip != 'true'"


def test_a_published_image_without_provenance_fails_instead_of_being_attested():
    """A digest under the tag that this workflow cannot vouch for stops the run.

    An interrupted publication and an out-of-band push look the same here.
    Attesting the existing digest would sign build provenance for an image
    this run never built, and Promote Release would then accept it. The run
    fails closed instead, and never attests anything it did not push.
    """
    guard = _step("existing")
    run = guard["run"]
    # The guard checks the binding Promote Release verifies before skipping.
    for flag in ("--signer-workflow", "publish-container.yml",
                 '--source-ref "refs/tags/$RELEASE_TAG"', "--source-digest"):
        assert flag in run
    verify = run.index("gh attestation verify")
    # Unverifiable: fail before reporting the tag as safely published.
    assert run.index("exit 1", verify) < run.index("skip=true")
    assert "attested=" not in run
    steps = PUBLISH["steps"]
    attest = next(st for st in steps if "attest-build-provenance" in st.get("uses", ""))
    assert attest["if"] == "steps.existing.outputs.skip != 'true'"
    assert attest["with"]["subject-digest"] == "${{ steps.push.outputs.digest }}"
    assert "existing" not in attest["with"]["subject-digest"]
    # Never a rebuild: the push stays gated on skip alone.
    assert _step("push")["if"] == "steps.existing.outputs.skip != 'true'"


_DIGEST = "sha256:" + "a" * 64
_FAKE_DOCKER = f"""#!/bin/sh
[ "$FAKE_IMAGE" = present ] || exit 1
echo '{{"digest":"{_DIGEST}"}}'
"""
_FAKE_GH = """#!/bin/sh
case "$1" in
  attestation)
    [ "$FAKE_VERIFY" = ok ] && exit 0
    echo "verification failed: $FAKE_VERIFY" >&2; exit 1 ;;
  api)
    case "$FAKE_API" in
      404) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
      down) echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1 ;;
      *) echo "$FAKE_API"; exit 0 ;;
    esac ;;
esac
exit 2
"""


def _run_guard(tmp_path, *, image="present", verify="ok", api="0"):
    """Run the guard's own script, under the shell GitHub uses, with gh and docker faked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", _FAKE_DOCKER), ("gh", _FAKE_GH)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "cirwel/unitares",
        "RELEASE_TAG": "v9.9.9",
        "REPOSITORY": "ghcr.io/cirwel/unitares",
        "FAKE_IMAGE": image,
        "FAKE_VERIFY": verify,
        "FAKE_API": api,
    }
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _step("existing")["run"]],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr, output.read_text()


DELETE_ADVICE = "Delete that package version in GHCR"


@pytest.mark.parametrize("case, kwargs, code, says, skip", [
    ("new release", {"image": "absent"}, 0, None, "skip=false"),
    ("attested release", {"verify": "ok"}, 0, "already published", "skip=true"),
    ("no attestation (empty list)", {"verify": "none", "api": "0"}, 1, DELETE_ADVICE, None),
    ("no attestation (404)", {"verify": "none", "api": "404"}, 1, DELETE_ADVICE, None),
    ("foreign attestation", {"verify": "mismatch", "api": "2"}, 1, "Investigate before deleting", None),
    ("check could not run", {"verify": "timeout", "api": "down"}, 1, "do not delete the image", None),
])
def test_the_guard_names_its_remedy_only_for_the_case_it_found(
        tmp_path, case, kwargs, code, says, skip):
    """Deleting a release image is the remedy only when this repository holds
    no attestation for it. A verify that failed for another reason, including
    a check that could not run, must never print that advice, and the
    verifier's own output stays visible."""
    rc, out, github_output = _run_guard(tmp_path, **kwargs)
    assert rc == code, (case, out)
    if says:
        assert says in out, (case, out)
    if says != DELETE_ADVICE:
        assert DELETE_ADVICE not in out, (case, out)
    if skip:
        assert skip in github_output, (case, github_output)
    else:
        assert "skip=" not in github_output, (case, github_output)
    if kwargs.get("verify", "ok") != "ok" and kwargs.get("image") != "absent":
        assert "verification failed" in out, (case, out)


def test_database_image_check_rebuilds_on_every_build_input():
    check = yaml.safe_load((ROOT / ".github/workflows/postgres-image.yml").read_text())
    events = check.get("on", check.get(True))
    for event in ("push", "pull_request"):
        paths = set(events[event]["paths"])
        assert {
            "db/postgres/Dockerfile.age-vector",
            "db/postgres/init-extensions.sql",
            ".github/workflows/publish-container.yml",
        } <= paths
    build = next(
        step for step in check["jobs"]["build"]["steps"]
        if "build-push-action" in step.get("uses", "")
    )
    assert build["with"]["context"] == "db/postgres"
    assert build["with"]["file"] == "db/postgres/Dockerfile.age-vector"
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert build["with"]["push"] is False


def test_lease_plane_image_check_rebuilds_on_every_build_input():
    check = yaml.safe_load((ROOT / ".github/workflows/lease-plane-image.yml").read_text())
    events = check.get("on", check.get(True))
    for event in ("push", "pull_request"):
        paths = set(events[event]["paths"])
        assert {
            "elixir/lease_plane/**",
            "elixir/unitares_sdk/**",
            ".dockerignore",
            ".github/workflows/publish-container.yml",
        } <= paths
