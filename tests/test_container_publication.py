"""Prevent release publication/backfill from moving the default install image."""
from pathlib import Path

import yaml


def test_publication_only_tags_the_versioned_artifact():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/publish-container.yml").read_text())
    # PyYAML's YAML 1.1 parser reads the GitHub Actions `on` key as True.
    events = workflow.get("on", workflow.get(True))
    assert events["release"]["types"] == ["published"]
    assert set(events["workflow_dispatch"]["inputs"]) == {"ref"}
    steps = workflow["jobs"]["publish"]["steps"]
    metadata = next(step for step in steps if step.get("id") == "meta")
    assert metadata["with"]["flavor"] == "latest=false"
    assert metadata["with"]["tags"].splitlines() == [
        "type=raw,value=${{ steps.release.outputs.tag }}"
    ]
    push = next(step for step in steps if step.get("id") == "push")
    assert push["with"]["tags"] == "${{ steps.meta.outputs.tags }}"
    assert push["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert push["with"]["sbom"] is True
    assert push["with"]["provenance"] == "mode=max"
