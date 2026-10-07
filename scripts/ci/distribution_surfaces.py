#!/usr/bin/env python3
"""Check every UNITARES distribution surface against what is live.

The registry is ``docs/operations/distribution-surfaces.toml``: every catalog
pin, awesome-list line, hosted listing, marketplace manifest, package, repo
description and site that describes UNITARES. Its header says why it exists.

Two modes:

``--offline`` (every PR, via ``tests/test_distribution_surfaces.py``)
    The registry parses and is well formed, every local path and
    ``covered_by`` check it names exists, the contract-test pin is readable,
    and every distribution manifest tracked in this repo is registered. That
    last check is the one that keeps the list complete: a new ``server.json``
    or ``smithery.yaml`` cannot land without an entry.

default (weekly, ``.github/workflows/positioning-surfaces.yml``)
    Runs each surface's probe against the live surface and prints one line per
    surface. Needs ``gh`` (authenticated) and network.

Statuses:
    OK       the live surface matches
    PENDING  it does not match yet, and an open PR moves it
    INFO     report only (descriptions are printed so copy drift is visible)
    SKIP     could not check (no gh, no auth, no network) -- never a pass
    DRIFT    the live surface disagrees and nothing in flight fixes it

Exit 1 on any DRIFT or offline error; otherwise 0. As in
``check_repo_description.py``, not-run is deliberately not a failure: a check
that is red whenever a token is missing teaches people to ignore it. Skips
are printed, so an unverified surface reports as unverified, never as passing.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "docs" / "operations" / "distribution-surfaces.toml"

KINDS = {
    "upstream-pin",
    "upstream-listing",
    "hosted-listing",
    "marketplace",
    "package",
    "repo-metadata",
    "site",
    "citation",
}

#: probe type -> fields it requires
PROBE_FIELDS: dict[str, tuple[str, ...]] = {
    "catalog-pin": ("repo", "path", "source_repo", "pin_file", "pin_pattern"),
    "upstream-listing": ("repo", "path", "needle", "pr"),
    "manifest-versions": ("repo", "files"),
    "repo-description": ("repos",),
    "http-ok": ("url",),
    "manual": ("last_verified", "max_age_days"),
    "covered": (),
}

#: File names that publish UNITARES somewhere when they appear in this repo.
#: Each tracked file with one of these names must be listed in some
#: surface's ``paths``.
MANIFEST_NAMES = {
    "glama.json",
    "server.json",
    "smithery.yaml",
    "smithery.yml",
    "llms.txt",
    "CITATION.cff",
    "Dockerfile.glama",
    "build-spec.json",
    "plugin.json",
    "plugin.yaml",
    "marketplace.json",
    "mcp.json",
    ".mcp.json",
}

#: Trees whose files are test data or vendored, never a published manifest.
MANIFEST_EXCLUDED_PREFIXES = ("tests/", "node_modules/", "elixir/deps/")


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    status: str  # OK | PENDING | INFO | SKIP | DRIFT
    surface: str
    message: str

    def line(self) -> str:
        return f"{self.status:<8} {self.surface}: {self.message}"


# --------------------------------------------------------------------------
# Network seam (tests substitute a fake)
# --------------------------------------------------------------------------


class Net(Protocol):
    def gh_json(self, endpoint: str) -> Any | None: ...

    def http_status(self, url: str) -> int | None: ...


class LiveNet:
    """``gh api`` for GitHub, urllib for everything else. None means not-run."""

    def __init__(self) -> None:
        self.has_gh = shutil.which("gh") is not None

    def gh_json(self, endpoint: str) -> Any | None:
        if not self.has_gh:
            return None
        try:
            proc = subprocess.run(
                ["gh", "api", endpoint],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

    def http_status(self, url: str) -> int | None:
        # curl first: a python.org macOS build ships without a CA bundle, so
        # urllib fails every TLS handshake there and every site would SKIP.
        if shutil.which("curl"):
            try:
                proc = subprocess.run(
                    ["curl", "-s", "-L", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "20", "-A", "unitares-distribution-surfaces/1", url],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            code = proc.stdout.strip()
            return int(code) if code.isdigit() and code != "000" else None
        request = urllib.request.Request(
            url, headers={"User-Agent": "unitares-distribution-surfaces/1"}
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (urllib.error.URLError, TimeoutError, OSError):
            return None


def _gh_file(net: Net, repo: str, path: str) -> str | None:
    payload = net.gh_json(f"repos/{repo}/contents/{path}")
    if not isinstance(payload, dict) or "content" not in payload:
        return None
    return base64.b64decode(payload["content"]).decode("utf-8", "replace")


_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _version_key(name: str) -> tuple[int, int, int] | None:
    match = _SEMVER.match(name.strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _latest_tag(net: Net, repo: str) -> tuple[str, str] | None:
    """(tag name, commit sha) of the highest plain-semver tag, or None."""
    tags = net.gh_json(f"repos/{repo}/tags?per_page=100")
    if not isinstance(tags, list):
        return None
    releases = [
        (key, tag["name"], tag["commit"]["sha"])
        for tag in tags
        if (key := _version_key(tag.get("name", ""))) is not None
    ]
    if not releases:
        return None
    _key, name, sha = max(releases)
    return name, sha


def _canonical_tagline() -> str:
    spec = importlib.util.spec_from_file_location(
        "_distribution_surfaces_doc_drift",
        REPO_ROOT / "scripts" / "diagnostics" / "check_doc_drift.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _label, alternatives = module.CANONICAL_TAGLINE
    return alternatives[0]


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


def probe_catalog_pin(sid: str, probe: dict, net: Net, root: Path) -> list[Result]:
    text = _gh_file(net, probe["repo"], probe["path"])
    if text is None:
        return [Result("SKIP", sid, f"could not read {probe['repo']}:{probe['path']}")]
    sha_match = re.search(r"^sha:\s*([0-9a-f]{40})", text, re.MULTILINE)
    version_match = re.search(r'^version:\s*"?([^"\n]+)"?', text, re.MULTILINE)
    if not sha_match:
        return [Result("DRIFT", sid, f"{probe['path']} carries no 40-char sha")]
    pinned = sha_match.group(1)
    pinned_version = version_match.group(1).strip() if version_match else "?"

    results: list[Result] = []
    local = (root / probe["pin_file"]).read_text(encoding="utf-8")
    local_match = re.search(probe["pin_pattern"], local)
    local_pin = local_match.group(1) if local_match else None
    if local_pin != pinned:
        results.append(
            Result(
                "DRIFT",
                sid,
                f"catalog pins {pinned[:12]} ({pinned_version}) but "
                f"{probe['pin_file']} exercises {str(local_pin)[:12]}; move the "
                "contract-test pin and payloads to the catalog's commit",
            )
        )

    latest = _latest_tag(net, probe["source_repo"])
    if latest is None:
        results.append(Result("SKIP", sid, f"could not read {probe['source_repo']} tags"))
        return results
    tag, tag_sha = latest
    if tag_sha == pinned:
        results.append(Result("OK", sid, f"catalog pins {tag} ({pinned[:12]}), the latest tag"))
        return results

    open_prs = _open_prs_touching(net, probe["repo"], probe["path"])
    if open_prs is None:
        results.append(Result("SKIP", sid, f"catalog at {pinned_version}, latest tag {tag}; could not list open PRs"))
    elif open_prs:
        results.append(
            Result(
                "PENDING",
                sid,
                f"catalog at {pinned_version}, latest tag {tag}; in flight: "
                + ", ".join(open_prs),
            )
        )
    else:
        results.append(
            Result(
                "DRIFT",
                sid,
                f"catalog at {pinned_version} ({pinned[:12]}), latest tag {tag} "
                f"({tag_sha[:12]}), and no open PR to {probe['repo']} moves it",
            )
        )
    return results


def _open_prs_touching(net: Net, repo: str, path: str) -> list[str] | None:
    """URLs of open PRs titled for unitares that change ``path``.

    Searched by title, not author: the workflow's GITHUB_TOKEN cannot read
    ``/user``, and who opened the bump does not matter, only that one is open.
    """
    found = net.gh_json(
        f"search/issues?q=repo:{repo}+is:pr+is:open+unitares+in:title&per_page=50"
    )
    if not isinstance(found, dict):
        return None
    urls = []
    for item in found.get("items", []):
        files = net.gh_json(f"repos/{repo}/pulls/{item['number']}/files?per_page=100")
        if isinstance(files, list) and any(f.get("filename") == path for f in files):
            urls.append(item["html_url"])
    return urls


def probe_upstream_listing(sid: str, probe: dict, net: Net, _root: Path) -> list[Result]:
    text = _gh_file(net, probe["repo"], probe["path"])
    if text is None:
        return [Result("SKIP", sid, f"could not read {probe['repo']}:{probe['path']}")]
    if probe["needle"].casefold() in text.casefold():
        return [Result("OK", sid, f"listed in {probe['repo']}")]
    pr = net.gh_json(f"repos/{probe['repo']}/pulls/{probe['pr']}")
    if not isinstance(pr, dict):
        return [Result("SKIP", sid, f"not listed yet; could not read PR #{probe['pr']}")]
    url = pr.get("html_url", f"#{probe['pr']}")
    if pr.get("state") == "open":
        return [Result("PENDING", sid, f"not listed yet; PR open: {url}")]
    if pr.get("merged_at"):
        return [Result("DRIFT", sid, f"PR merged ({url}) but {probe['needle']!r} is gone from {probe['path']}")]
    return [Result("DRIFT", sid, f"PR closed unmerged ({url}); not listed. Reopen, resubmit, or move this entry to [[not_listed]]")]


def _collect(obj: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str):
                found.append(v)
            else:
                found.extend(_collect(v, key))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_collect(item, key))
    return found


def probe_manifest_versions(sid: str, probe: dict, net: Net, _root: Path) -> list[Result]:
    repo = probe["repo"]
    versions: dict[str, list[str]] = {}
    descriptions: dict[str, list[str]] = {}
    for path in probe["files"]:
        text = _gh_file(net, repo, path)
        if text is None:
            return [Result("SKIP", sid, f"could not read {repo}:{path}")]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [Result("DRIFT", sid, f"{repo}:{path} is not valid JSON")]
        for version in _collect(data, "version"):
            versions.setdefault(version, []).append(path)
        for description in _collect(data, "description"):
            descriptions.setdefault(description, []).append(path)

    results: list[Result] = []
    if len(versions) > 1:
        detail = "; ".join(f"{v} in {', '.join(sorted(set(p)))}" for v, p in versions.items())
        results.append(Result("DRIFT", sid, f"manifests disagree on version: {detail}"))
    elif versions:
        (version,) = versions
        latest = _latest_tag(net, repo)
        own, tagged = _version_key(version), _version_key(latest[0]) if latest else None
        if latest is None or own is None or tagged is None:
            results.append(Result("SKIP", sid, f"manifests at {version}; could not compare with tags"))
        elif own == tagged:
            results.append(Result("OK", sid, f"every manifest at {version}, the latest tag"))
        elif own > tagged:
            results.append(Result("PENDING", sid, f"manifests at {version}, ahead of tag {latest[0]} (release not cut)"))
        else:
            results.append(Result("DRIFT", sid, f"manifests at {version}, behind tag {latest[0]}"))
    for description, paths in descriptions.items():
        where = ", ".join(sorted(set(paths)))
        results.append(Result("INFO", sid, f"description [{where}]: {description}"))
    if len(descriptions) > 1:
        results.append(Result("INFO", sid, f"{len(descriptions)} different descriptions across manifests"))
    return results


def probe_repo_description(sid: str, probe: dict, net: Net, _root: Path) -> list[Result]:
    tagline = _canonical_tagline() if probe.get("must_contain_tagline") else None
    results: list[Result] = []
    for repo in probe["repos"]:
        payload = net.gh_json(f"repos/{repo}")
        if not isinstance(payload, dict):
            results.append(Result("SKIP", sid, f"could not read {repo}"))
            continue
        description = " ".join((payload.get("description") or "").split())
        if tagline is None:
            results.append(Result("INFO", sid, f"{repo}: {description or '(no description)'}"))
        elif tagline.casefold() in description.casefold():
            results.append(Result("OK", sid, f"{repo} carries the canonical tagline"))
        else:
            results.append(Result("DRIFT", sid, f"{repo} reads {description!r}; expected to contain {tagline!r}"))
    return results


def _http_result(sid: str, url: str, net: Net) -> Result:
    status = net.http_status(url)
    if status is None:
        return Result("SKIP", sid, f"could not reach {url}")
    if 200 <= status < 400:
        return Result("OK", sid, f"{url} answers {status}")
    if status in (404, 410):
        return Result("DRIFT", sid, f"{url} answers {status}: the surface is gone")
    return Result("SKIP", sid, f"{url} answers {status}")


def probe_http_ok(sid: str, probe: dict, net: Net, _root: Path) -> list[Result]:
    return [_http_result(sid, probe["url"], net)]


def probe_manual(
    sid: str, probe: dict, net: Net, _root: Path, today: dt.date | None = None
) -> list[Result]:
    today = today or dt.date.today()
    verified = dt.date.fromisoformat(probe["last_verified"])
    age = (today - verified).days
    results: list[Result] = []
    if "url" in probe:
        reached = _http_result(sid, probe["url"], net)
        if reached.status == "DRIFT":
            results.append(reached)
    if age > probe["max_age_days"]:
        results.append(
            Result(
                "DRIFT",
                sid,
                f"last verified {verified} ({age} days, limit {probe['max_age_days']}); "
                "look at the listing, then update last_verified and observed",
            )
        )
    else:
        results.append(Result("OK", sid, f"verified by hand {verified}: {probe.get('observed', '')}".rstrip(": ")))
    return results


def probe_covered(sid: str, surface: dict, _net: Net, _root: Path) -> list[Result]:
    owners = ", ".join(surface.get("covered_by", [])) or "(none named)"
    return [Result("OK", sid, f"covered by {owners}")]


PROBES = {
    "catalog-pin": probe_catalog_pin,
    "upstream-listing": probe_upstream_listing,
    "manifest-versions": probe_manifest_versions,
    "repo-description": probe_repo_description,
    "http-ok": probe_http_ok,
    "manual": probe_manual,
}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def load_registry(path: Path = REGISTRY) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _tracked_files(root: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return proc.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]


def offline_errors(registry: dict, root: Path = REPO_ROOT) -> list[str]:
    errors: list[str] = []
    if registry.get("schema") != 1:
        errors.append("registry: schema must be 1")

    surfaces = registry.get("surface", [])
    seen: set[str] = set()
    registered: set[str] = set()
    for surface in surfaces:
        sid = surface.get("id", "<missing id>")
        if sid in seen:
            errors.append(f"{sid}: duplicate id")
        seen.add(sid)
        for key in ("id", "kind", "title", "where", "refresh", "probe"):
            if key not in surface:
                errors.append(f"{sid}: missing {key}")
        if surface.get("kind") not in KINDS:
            errors.append(f"{sid}: unknown kind {surface.get('kind')!r}")

        probe = surface.get("probe", {})
        ptype = probe.get("type")
        if ptype not in PROBE_FIELDS:
            errors.append(f"{sid}: unknown probe type {ptype!r}")
        else:
            for field in PROBE_FIELDS[ptype]:
                if field not in probe:
                    errors.append(f"{sid}: probe {ptype} needs {field}")
        if not isinstance(surface.get("content_checked_by", []), list):
            errors.append(f"{sid}: content_checked_by must be a list")
        if ptype == "covered" and not surface.get("covered_by"):
            errors.append(f"{sid}: probe 'covered' needs a covered_by list")
        if ptype == "manual" and "last_verified" in probe:
            try:
                dt.date.fromisoformat(probe["last_verified"])
            except ValueError:
                errors.append(f"{sid}: last_verified is not an ISO date")
        if ptype == "catalog-pin" and "pin_file" in probe:
            pin_path = root / probe["pin_file"]
            registered.add(probe["pin_file"])
            if not pin_path.is_file():
                errors.append(f"{sid}: pin_file {probe['pin_file']} does not exist")
            elif not re.search(probe.get("pin_pattern", "(?!)"), pin_path.read_text(encoding="utf-8")):
                errors.append(f"{sid}: pin_pattern finds no pin in {probe['pin_file']}")

        for rel in [*surface.get("paths", []), *surface.get("covered_by", [])]:
            registered.add(rel)
            if not (root / rel).exists():
                errors.append(f"{sid}: {rel} does not exist")

    for entry in registry.get("not_listed", []):
        for key in ("where", "checked", "note"):
            if key not in entry:
                errors.append(f"not_listed {entry.get('where', '?')}: missing {key}")

    for rel in _tracked_files(root):
        if rel.startswith(MANIFEST_EXCLUDED_PREFIXES):
            continue
        if Path(rel).name in MANIFEST_NAMES and rel not in registered:
            errors.append(
                f"unregistered distribution manifest: {rel} "
                f"(add it to a surface's paths in {REGISTRY.relative_to(REPO_ROOT)})"
            )
    return errors


def run_probes(
    registry: dict, net: Net, root: Path = REPO_ROOT, only: str | None = None
) -> list[Result]:
    results: list[Result] = []
    for surface in registry.get("surface", []):
        sid = surface["id"]
        if only and sid != only:
            continue
        probe = surface["probe"]
        if probe["type"] == "covered":
            results.extend(probe_covered(sid, surface, net, root))
        else:
            results.extend(PROBES[probe["type"]](sid, probe, net, root))
        if "content_checked_by" in surface:
            readers = surface["content_checked_by"]
            text = "; ".join(readers) if readers else "nothing reads this surface's words"
            results.append(Result("INFO", sid, f"content checked by: {text}"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="validate the registry only; no network")
    parser.add_argument("--only", metavar="ID", help="probe one surface")
    args = parser.parse_args(argv)

    registry = load_registry()
    errors = offline_errors(registry)
    for error in errors:
        print(f"ERROR    {error}")
    if args.offline:
        if not errors:
            count = len(registry.get("surface", []))
            print(f"OK       registry: {count} surfaces, every manifest registered")
        return 1 if errors else 0

    results = run_probes(registry, LiveNet(), only=args.only)
    for result in results:
        print(result.line())
    for entry in registry.get("not_listed", []):
        print(f"{'ABSENT':<8} {entry['where']} (checked {entry['checked']}): {entry['note']}")
    drift = [r for r in results if r.status == "DRIFT"]
    skipped = [r for r in results if r.status == "SKIP"]
    print(
        f"\n{len(results)} results: {len(drift)} drift, "
        f"{sum(r.status == 'PENDING' for r in results)} pending, {len(skipped)} skipped"
    )
    return 1 if errors or drift else 0


if __name__ == "__main__":
    sys.exit(main())
