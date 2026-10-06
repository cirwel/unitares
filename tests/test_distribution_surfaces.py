"""The distribution-surface registry stays complete and its probes classify right.

The offline check runs here on every PR: it is what stops a new published
manifest from landing without an entry. The probe tests use a fake network so
each live-surface state (current, PR in flight, drifted) is pinned without
calling GitHub.
"""

from __future__ import annotations

import base64
import datetime as dt
import importlib.util
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "distribution_surfaces", PROJECT_ROOT / "scripts" / "ci" / "distribution_surfaces.py"
)
assert _SPEC and _SPEC.loader
ds = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ds  # dataclasses resolve their module through sys.modules
_SPEC.loader.exec_module(ds)

PIN = "a" * 40
NEW = "b" * 40


class FakeNet:
    def __init__(self, gh: dict | None = None, http: dict | None = None) -> None:
        self.gh = gh or {}
        self.http = http or {}

    def gh_json(self, endpoint: str):
        return self.gh.get(endpoint)

    def http_status(self, url: str):
        return self.http.get(url)


def _file(text: str) -> dict:
    return {"content": base64.b64encode(text.encode()).decode()}


def _statuses(results) -> list[str]:
    return [r.status for r in results]


# --------------------------------------------------------------------------
# offline
# --------------------------------------------------------------------------


def test_registry_is_complete_and_well_formed() -> None:
    assert ds.offline_errors(ds.load_registry()) == []


def test_unregistered_manifest_is_reported(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "server.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "plugin.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)

    errors = ds.offline_errors({"schema": 1, "surface": []}, root=tmp_path)

    assert errors == [
        "unregistered distribution manifest: server.json "
        "(add it to a surface's paths in docs/operations/distribution-surfaces.toml)"
    ]


def test_missing_path_and_bad_probe_are_reported(tmp_path: Path) -> None:
    registry = {
        "schema": 1,
        "surface": [
            {
                "id": "x",
                "kind": "site",
                "title": "t",
                "where": "w",
                "refresh": "r",
                "paths": ["nope.json"],
                "probe": {"type": "http-ok"},
            }
        ],
    }
    errors = ds.offline_errors(registry, root=tmp_path)
    assert "x: probe http-ok needs url" in errors
    assert "x: nope.json does not exist" in errors


# --------------------------------------------------------------------------
# catalog-pin
# --------------------------------------------------------------------------

CATALOG = {
    "type": "catalog-pin",
    "repo": "up/hermes",
    "path": "plugin-catalog/unitares.yaml",
    "source_repo": "me/adapter",
    "pin_file": "pin.py",
    "pin_pattern": 'CATALOG_PIN = "([0-9a-f]{40})"',
}


def _catalog_net(latest_sha: str, open_pr_files: list[str] | None) -> FakeNet:
    gh = {
        "repos/up/hermes/contents/plugin-catalog/unitares.yaml": _file(
            f'sha: {PIN}\nversion: "0.3.1"\n'
        ),
        "repos/me/adapter/tags?per_page=100": [
            {"name": "v0.3.1", "commit": {"sha": PIN}},
            {"name": "v0.3.3", "commit": {"sha": latest_sha}},
            {"name": "v0.4.0a1", "commit": {"sha": "c" * 40}},
        ],
        "search/issues?q=repo:up/hermes+is:pr+is:open+unitares+in:title&per_page=50": {
            "items": [] if open_pr_files is None else [{"number": 7, "html_url": "https://pr/7"}]
        },
    }
    if open_pr_files is not None:
        gh["repos/up/hermes/pulls/7/files?per_page=100"] = [
            {"filename": f} for f in open_pr_files
        ]
    return FakeNet(gh)


def _pin_root(tmp_path: Path, pin: str = PIN) -> Path:
    (tmp_path / "pin.py").write_text(f'CATALOG_PIN = "{pin}"\n', encoding="utf-8")
    return tmp_path


def test_catalog_on_latest_tag_is_ok(tmp_path: Path) -> None:
    results = ds.probe_catalog_pin("c", CATALOG, _catalog_net(PIN, None), _pin_root(tmp_path))
    assert _statuses(results) == ["OK"]


def test_catalog_behind_with_open_bump_is_pending(tmp_path: Path) -> None:
    net = _catalog_net(NEW, ["plugin-catalog/unitares.yaml"])
    results = ds.probe_catalog_pin("c", CATALOG, net, _pin_root(tmp_path))
    assert _statuses(results) == ["PENDING"]
    assert "https://pr/7" in results[0].message


def test_catalog_behind_with_unrelated_pr_is_drift(tmp_path: Path) -> None:
    net = _catalog_net(NEW, ["cli/main.py"])
    results = ds.probe_catalog_pin("c", CATALOG, net, _pin_root(tmp_path))
    assert _statuses(results) == ["DRIFT"]


def test_contract_test_pin_off_the_catalog_is_drift(tmp_path: Path) -> None:
    results = ds.probe_catalog_pin(
        "c", CATALOG, _catalog_net(PIN, None), _pin_root(tmp_path, pin=NEW)
    )
    assert _statuses(results) == ["DRIFT", "OK"]
    assert "pin.py" in results[0].message


# --------------------------------------------------------------------------
# upstream-listing
# --------------------------------------------------------------------------

LISTING = {"type": "upstream-listing", "repo": "o/list", "path": "README.md", "needle": "cirwel/unitares", "pr": 3}


def _listing_net(readme: str, pr: dict | None) -> FakeNet:
    gh = {"repos/o/list/contents/README.md": _file(readme)}
    if pr is not None:
        gh["repos/o/list/pulls/3"] = {"html_url": "https://pr/3", **pr}
    return FakeNet(gh)


def test_listed_is_ok() -> None:
    net = _listing_net("- [CIRWEL/unitares](x)", None)
    assert _statuses(ds.probe_upstream_listing("l", LISTING, net, PROJECT_ROOT)) == ["OK"]


def test_unlisted_with_open_pr_is_pending() -> None:
    net = _listing_net("nothing", {"state": "open", "merged_at": None})
    assert _statuses(ds.probe_upstream_listing("l", LISTING, net, PROJECT_ROOT)) == ["PENDING"]


def test_closed_unmerged_pr_is_drift() -> None:
    net = _listing_net("nothing", {"state": "closed", "merged_at": None})
    assert _statuses(ds.probe_upstream_listing("l", LISTING, net, PROJECT_ROOT)) == ["DRIFT"]


def test_merged_then_removed_is_drift() -> None:
    net = _listing_net("nothing", {"state": "closed", "merged_at": "2026-10-07T00:00:00Z"})
    assert _statuses(ds.probe_upstream_listing("l", LISTING, net, PROJECT_ROOT)) == ["DRIFT"]


# --------------------------------------------------------------------------
# manifest-versions, manual, unreachable
# --------------------------------------------------------------------------

MANIFESTS = {"type": "manifest-versions", "repo": "o/plugin", "files": ["a.json", "b.json"]}


def _manifest_net(a: str, b: str, tag: str) -> FakeNet:
    return FakeNet(
        {
            "repos/o/plugin/contents/a.json": _file(f'{{"version": "{a}", "description": "one"}}'),
            "repos/o/plugin/contents/b.json": _file(
                f'{{"plugins": [{{"version": "{b}", "description": "one"}}]}}'
            ),
            "repos/o/plugin/tags?per_page=100": [{"name": tag, "commit": {"sha": PIN}}],
        }
    )


def test_manifests_agreeing_on_latest_tag_are_ok() -> None:
    results = ds.probe_manifest_versions("m", MANIFESTS, _manifest_net("0.4.19", "0.4.19", "v0.4.19"), PROJECT_ROOT)
    assert _statuses(results) == ["OK", "INFO"]


def test_manifests_disagreeing_is_drift() -> None:
    results = ds.probe_manifest_versions("m", MANIFESTS, _manifest_net("0.4.20", "0.4.19", "v0.4.19"), PROJECT_ROOT)
    assert results[0].status == "DRIFT"


def test_manifests_ahead_of_tag_is_pending() -> None:
    results = ds.probe_manifest_versions("m", MANIFESTS, _manifest_net("0.4.20", "0.4.20", "v0.4.19"), PROJECT_ROOT)
    assert results[0].status == "PENDING"


def test_manual_check_goes_stale() -> None:
    probe = {"type": "manual", "last_verified": "2026-10-06", "max_age_days": 45}
    fresh = ds.probe_manual("g", probe, FakeNet(), PROJECT_ROOT, today=dt.date(2026, 11, 1))
    stale = ds.probe_manual("g", probe, FakeNet(), PROJECT_ROOT, today=dt.date(2026, 12, 1))
    assert _statuses(fresh) == ["OK"]
    assert _statuses(stale) == ["DRIFT"]


def test_unreachable_is_skip_and_gone_is_drift() -> None:
    probe = {"type": "http-ok", "url": "https://x"}
    assert _statuses(ds.probe_http_ok("h", probe, FakeNet(http={}), PROJECT_ROOT)) == ["SKIP"]
    assert _statuses(ds.probe_http_ok("h", probe, FakeNet(http={"https://x": 404}), PROJECT_ROOT)) == ["DRIFT"]


def test_content_readers_are_reported_including_none() -> None:
    registry = {
        "surface": [
            {"id": "read", "probe": {"type": "covered"}, "covered_by": ["x"], "content_checked_by": ["claims check"]},
            {"id": "unread", "probe": {"type": "covered"}, "covered_by": ["x"], "content_checked_by": []},
        ]
    }
    info = [r.message for r in ds.run_probes(registry, FakeNet()) if r.status == "INFO"]
    assert info == [
        "content checked by: claims check",
        "content checked by: nothing reads this surface's words",
    ]
