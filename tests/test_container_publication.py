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


def test_one_release_publishes_the_server_and_the_lease_plane():
    """docker-compose.yml pins ghcr.io/<repo>-lease-plane to the release tag."""
    strategy = PUBLISH["strategy"]
    # A lease-plane failure must not cancel the server image, or the reverse.
    assert strategy["fail-fast"] is False
    artifacts = {entry["artifact"]: entry for entry in strategy["matrix"]["include"]}
    assert set(artifacts) == {"server", "lease-plane"}
    assert artifacts["server"]["image_suffix"] == ""
    assert artifacts["server"]["dockerfile"] == "Dockerfile"
    assert artifacts["lease-plane"]["image_suffix"] == "-lease-plane"
    assert artifacts["lease-plane"]["dockerfile"] == "elixir/lease_plane/Dockerfile"

    image = "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}${{ matrix.image_suffix }}"
    assert _step("meta")["with"]["images"] == image
    push = _step("push")
    assert push["with"]["context"] == "."
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


def test_a_published_lease_plane_tag_is_never_replaced():
    """Compose pulls the lease plane by tag; a re-dispatch must not rebuild
    over the digest Promote Release verified."""
    guard = _step("existing")
    assert guard["if"] == "matrix.artifact == 'lease-plane'"
    assert "docker buildx imagetools inspect" in guard["run"]
    assert 'skip=true' in guard["run"]
    steps = PUBLISH["steps"]
    login = next(i for i, st in enumerate(steps) if "login-action" in st.get("uses", ""))
    assert login < steps.index(guard) < steps.index(_step("push"))
    assert _step("push")["if"] == "steps.existing.outputs.skip != 'true'"
    attest = next(st for st in steps if "attest-build-provenance" in st.get("uses", ""))
    assert attest["if"] == "steps.existing.outputs.skip != 'true'"


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
