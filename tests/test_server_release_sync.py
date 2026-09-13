from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))

from server_release_sync import assess  # noqa: E402

A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64
C = "sha256:" + "c" * 64


def _codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


def test_converged_release_surfaces_are_synced():
    report = assess(
        source="2.23.0",
        published="2.23.0",
        tagged={"2.22.1", "2.23.0"},
        on_head={"2.22.1", "2.23.0"},
        released={"2.22.1", "2.23.0"},
        digests={"latest": B, "v2.23.0": B, "v2.22.1": A},
    )
    assert report["synced"] is True
    assert report["issues"] == []


def test_source_may_lead_the_newest_tag_between_releases():
    """Master labelled for a release being prepared is not drift."""
    report = assess(
        source="2.24.0",
        published="2.23.0",
        tagged={"2.23.0"},
        on_head={"2.23.0"},
        released={"2.23.0"},
        digests={"latest": B, "v2.23.0": B},
    )
    assert report["synced"] is True


def test_stalled_maintenance_release_reports_every_split():
    """The 2026-09-13 state: v2.22.1 tagged off-master and never published."""
    report = assess(
        source="2.22.0",
        published="2.21.0",
        tagged={"2.21.0", "2.22.0", "2.22.1"},
        on_head={"2.21.0", "2.22.0"},
        released={"2.21.0", "2.22.0", "2.22.1"},
        digests={"latest": B, "v2.22.1": C, "v2.22.0": B, "v2.21.0": A},
    )
    assert report["synced"] is False
    assert _codes(report) == {
        "latest_not_published_version",
        "release_awaiting_publication",
        "newest_tag_not_on_master",
        "source_version_behind_tag",
    }
    latest = next(i for i in report["issues"] if i["code"] == "latest_not_published_version")
    assert "latest serves v2.22.0" in latest["detail"]


def test_forward_merge_leaves_only_the_deferred_publication():
    report = assess(
        source="2.22.1",
        published="2.21.0",
        tagged={"2.21.0", "2.22.0", "2.22.1"},
        on_head={"2.21.0", "2.22.0", "2.22.1"},
        released={"2.21.0", "2.22.0", "2.22.1"},
        digests={"latest": B, "v2.22.1": C, "v2.22.0": B, "v2.21.0": A},
    )
    assert _codes(report) == {"latest_not_published_version", "release_awaiting_publication"}


def test_published_version_without_its_artifacts_is_visible():
    report = assess(
        source="2.23.0",
        published="2.23.0",
        tagged={"2.22.1"},
        on_head={"2.22.1"},
        released={"2.22.1"},
        digests={"latest": A, "v2.23.0": None, "v2.22.1": A},
    )
    assert {
        "published_tag_missing",
        "published_release_page_missing",
        "published_image_missing",
    } <= _codes(report)


def test_latest_on_an_untracked_image_names_no_tag():
    report = assess(
        source="2.23.0",
        published="2.23.0",
        tagged={"2.23.0"},
        on_head={"2.23.0"},
        released={"2.23.0"},
        digests={"latest": C, "v2.23.0": B},
    )
    issue = next(i for i in report["issues"] if i["code"] == "latest_not_published_version")
    assert "an image no checked tag names" in issue["detail"]


def test_missing_latest_is_not_collapsed_to_clean():
    report = assess(
        source="2.23.0",
        published="2.23.0",
        tagged={"2.23.0"},
        on_head={"2.23.0"},
        released={"2.23.0"},
        digests={"latest": None, "v2.23.0": B},
    )
    assert _codes(report) == {"latest_image_missing"}
