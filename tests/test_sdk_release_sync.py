from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))

from sdk_release_sync import (  # noqa: E402
    advertised_versions,
    assess,
    declared_version,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _advertised(version: str | None = "0.3.0") -> dict[str, str | None]:
    return {
        "COMPATIBILITY.md": version,
        "docs/public-site/index.md": version,
    }


def _codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


def test_all_four_release_surfaces_converge():
    report = assess(
        declared="0.3.0",
        published="0.3.0",
        tagged={"0.2.2", "0.3.0"},
        advertised=_advertised(),
    )
    assert report["synced"] is True
    assert report["status"] == "synced"
    assert report["issues"] == []


def test_in_tree_bump_without_tag_or_pypi_release_is_visible():
    """The 0.3.0 failure mode: intent advanced and every consumer stayed stale."""
    report = assess(
        declared="0.4.0",
        published="0.3.0",
        tagged={"0.3.0"},
        advertised=_advertised(),
    )
    assert report["synced"] is False
    assert _codes(report) == {
        "declared_registry_mismatch",
        "declared_tag_missing",
    }


def test_tag_without_completed_registry_publish_is_still_drift():
    report = assess(
        declared="0.4.0",
        published="0.3.0",
        tagged={"0.3.0", "0.4.0"},
        advertised=_advertised(),
    )
    assert _codes(report) == {"declared_registry_mismatch"}


def test_registry_release_without_its_tag_is_visible():
    report = assess(
        declared="0.3.0",
        published="0.3.0",
        tagged={"0.2.2"},
        advertised=_advertised(),
    )
    assert _codes(report) == {"declared_tag_missing", "published_tag_missing"}


def test_stale_and_missing_public_claims_are_both_visible():
    report = assess(
        declared="0.3.0",
        published="0.3.0",
        tagged={"0.3.0"},
        advertised={
            "COMPATIBILITY.md": "0.2.2",
            "docs/public-site/index.md": None,
        },
    )
    assert _codes(report) == {
        "public_claim_registry_mismatch",
        "public_claim_missing",
    }


def test_current_repository_version_and_claims_are_synchronized_without_network():
    declared = declared_version(REPO_ROOT)
    report = assess(
        declared=declared,
        published="0.3.0",
        # The general test workflow intentionally uses a shallow, tagless checkout.
        # Tag parsing is exercised through assess() above; the scheduled sentinel's
        # workflow contract below separately requires fetch-depth: 0.
        tagged={declared},
        advertised=advertised_versions(REPO_ROOT),
    )
    assert report["synced"] is True


def test_workflow_is_scheduled_deduplicated_and_self_closing():
    workflow = (REPO_ROOT / ".github/workflows/sdk-release-sync.yml").read_text()
    assert 'cron: "7 9 * * 1"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "fetch-depth: 0" in workflow
    assert "scripts/ci/sdk_release_sync.py --json" in workflow
    assert "finding-fingerprint: sdk-release-sync" in workflow
    assert "gh issue create" in workflow
    assert "gh issue comment" in workflow
    assert "gh issue close" in workflow
