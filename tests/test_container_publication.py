"""Prevent release publication/backfill from moving the default install image."""
from pathlib import Path

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
