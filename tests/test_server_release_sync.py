from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))

import io  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import urllib.error  # noqa: E402

import pytest  # noqa: E402

import server_release_sync  # noqa: E402
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
        "tag_not_on_master",
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


def test_unmerged_maintenance_tag_stays_visible_after_a_newer_release():
    report = assess(
        source="2.23.0",
        published="2.23.0",
        tagged={"2.22.1", "2.23.0"},
        on_head={"2.23.0"},
        released={"2.22.1", "2.23.0"},
        digests={"latest": B, "v2.23.0": B, "v2.22.1": A},
    )
    assert _codes(report) == {"tag_not_on_master"}
    assert "v2.22.1" in report["issues"][0]["detail"]


class _Response(io.BytesIO):
    def __init__(self, body: bytes = b"", headers: dict | None = None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", {}, None)


def test_image_digests_reads_the_header_and_maps_404_to_absent(monkeypatch):
    def fake_urlopen(request, timeout, context):
        url = request if isinstance(request, str) else request.full_url
        if "ghcr.io/token" in url:
            return _Response(json.dumps({"token": "t"}).encode())
        assert request.get_header("Authorization") == "Bearer t"
        if url.endswith("/latest"):
            return _Response(headers={"Docker-Content-Digest": A})
        raise _http_error(url, 404)

    monkeypatch.setattr(server_release_sync.urllib.request, "urlopen", fake_urlopen)
    assert server_release_sync.image_digests(["latest", "v9.9.9"]) == {
        "latest": A,
        "v9.9.9": None,
    }


def test_image_digests_refuses_a_response_without_a_digest(monkeypatch):
    def fake_urlopen(request, timeout, context):
        url = request if isinstance(request, str) else request.full_url
        if "ghcr.io/token" in url:
            return _Response(json.dumps({"token": "t"}).encode())
        return _Response(headers={})

    monkeypatch.setattr(server_release_sync.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError):
        server_release_sync.image_digests(["latest"])


def test_image_digests_raises_on_non_404_registry_errors(monkeypatch):
    def fake_urlopen(request, timeout, context):
        url = request if isinstance(request, str) else request.full_url
        if "ghcr.io/token" in url:
            return _Response(json.dumps({"token": "t"}).encode())
        raise _http_error(url, 500)

    monkeypatch.setattr(server_release_sync.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        server_release_sync.image_digests(["latest"])


def test_released_versions_looks_up_each_tag_and_skips_drafts(monkeypatch):
    seen: list[str] = []

    def fake_urlopen(request, timeout, context):
        seen.append(request.full_url)
        if request.full_url.endswith("/v2.21.0"):
            return _Response(json.dumps({"tag_name": "v2.21.0", "draft": False}).encode())
        if request.full_url.endswith("/v2.22.1"):
            return _Response(json.dumps({"tag_name": "v2.22.1", "draft": True}).encode())
        raise _http_error(request.full_url, 404)

    monkeypatch.setattr(server_release_sync.urllib.request, "urlopen", fake_urlopen)
    assert server_release_sync.released_versions({"2.21.0", "2.22.1", "2.99.0"}) == {"2.21.0"}
    assert all("/releases/tags/v" in url for url in seen)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_tags_and_ancestry_come_from_the_checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "tag", "v2.22.0")
    _git(repo, "switch", "-q", "-c", "release/2.22")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "maintenance")
    _git(repo, "tag", "v2.22.1")
    _git(repo, "tag", "sdk-v0.3.0")
    _git(repo, "switch", "-q", "master")

    assert server_release_sync.tagged_versions(repo) == {"2.22.0", "2.22.1"}
    assert server_release_sync.tag_on_head(repo, "2.22.0") is True
    assert server_release_sync.tag_on_head(repo, "2.22.1") is False


def test_unreadable_registry_is_indeterminate_not_synced(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("2.22.1\n")
    (tmp_path / "PUBLISHED_VERSION").write_text("2.21.0\n")
    monkeypatch.setattr(server_release_sync, "tagged_versions", lambda root: {"2.21.0", "2.22.1"})
    monkeypatch.setattr(server_release_sync, "tag_on_head", lambda root, version: True)

    def unreachable(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(server_release_sync, "released_versions", unreachable)
    report = server_release_sync.collect(tmp_path)
    assert report["status"] == "indeterminate"
    assert report["synced"] is False
    assert _codes(report) == {"registry_unreadable"}
