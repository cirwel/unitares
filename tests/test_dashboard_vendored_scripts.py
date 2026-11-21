"""Dashboard pages load third-party scripts only from the governance origin.

The dashboard pages run with the operator's session cookie, and a trusted
caller's page also receives the local API token. A script fetched from a CDN
runs with that same authority, so a changed file on the CDN (or a new release
behind a floating alias such as d3.v7) would run in the governance origin.
Chart.js and d3 are vendored under dashboard/redesign/vendor/ instead, and
this module pins them:

- no page under dashboard/ loads a script from another origin;
- every vendored file matches the SHA-512 recorded here, which is the file
  from the package's npm release tarball. The tarball's own integrity (npm
  ``dist.integrity``) is recorded beside each pin for provenance, and the same
  bytes were confirmed on jsDelivr, unpkg and cdnjs when they were added;
- each vendored script keeps its upstream licence header, with the licence
  file beside it;
- the paths the pages reference are actually served by the dashboard routes.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import pytest

from src.http_api import http_dashboard_redesign

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "dashboard"
VENDOR = DASHBOARD / "redesign" / "vendor"

# path under vendor/ -> SHA-512 (hex) of the file as committed.
VENDORED_SHA512 = {
    # chart.js 4.4.0: package/dist/chart.umd.js from
    # https://registry.npmjs.org/chart.js/-/chart.js-4.4.0.tgz, dist.integrity
    # sha512-vQEj6d+z0dcsKLlQvbKIMYFHd3t8W/7L2vfJIbYcfyPcRx92CsHqECpueN8qVGNlKyDcr5wBrYAYKnfu/9Q1hQ==
    "chart.js-4.4.0/chart.umd.js": (
        "e87acfa80bcafa5644948678999eb87f2c48053b1a5f9cc015983657fd964fe8"
        "8a3eb1734f3bf1e9002c2d6d8e68bc286e131806883ff9bbe62041065e2d29d7"
    ),
    "chart.js-4.4.0/LICENSE.md": (
        "f98ab35d2a892c463f3c82c94788c0339ee1262d47ac1def3491ec41610a5eb9"
        "0633c01170bc1b5a292d0a77713b6f08f7244acf5d68d4204746817d46d455cb"
    ),
    # d3 7.9.0: package/dist/d3.min.js from
    # https://registry.npmjs.org/d3/-/d3-7.9.0.tgz, dist.integrity
    # sha512-e1U46jVP+w7Iut8Jt8ri1YsPOvFpg46k+K8TpCb0P+zjCkjkPnV7WzfDJzMHy1LnA+wj5pLT1wjO901gLXeEhA==
    "d3-7.9.0/d3.min.js": (
        "bdce7caafbc176b0d1e1eb5bc4c7654ede06050935aa3bf23914769ebb0fb053"
        "f2aecfbfbb9737fb50adeaea4e81d668225edeaba9376b550f0d2340422ff7d8"
    ),
    "d3-7.9.0/LICENSE": (
        "25aee12620422c7cbf87e10af049c6a2b1ed00c46c32b699a4d342ee0a5f1ea5"
        "0e404453d037256cdc07f5977f5b4f7cadb2ea0141c49f0230173cf3cd7ea494"
    ),
}

# Upstream licence header each vendored script must still open with.
LICENCE_HEADERS = {
    "chart.js-4.4.0/chart.umd.js": (
        "/*!\n * Chart.js v4.4.0\n * https://www.chartjs.org\n"
        " * (c) 2023 Chart.js Contributors\n * Released under the MIT License\n */"
    ),
    "d3-7.9.0/d3.min.js": "// https://d3js.org v7.9.0 Copyright 2010-2023 Mike Bostock",
}

# Page -> the URL it serves from, and the vendored scripts it must load.
PAGES = {
    "redesign/app.html": ("/dashboard/redesign/", {"chart.js-4.4.0/chart.umd.js"}),
    "phase.html": ("/phase", {"d3-7.9.0/d3.min.js"}),
}


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    # Keep the redesign handler off its token-inject branch, which needs a
    # real request peer (see tests/test_dashboard_redesign_route.py).
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


class _ScriptSrcs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.srcs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            src = dict(attrs).get("src")
            if src is not None:
                self.srcs.append(src)


def _script_srcs(html: str) -> list[str]:
    parser = _ScriptSrcs()
    parser.feed(html)
    return parser.srcs


def _dashboard_pages() -> list[Path]:
    return sorted(
        p for p in DASHBOARD.rglob("*.html") if "node_modules" not in p.relative_to(DASHBOARD).parts
    )


def _is_cross_origin(src: str) -> bool:
    parts = urlsplit(src.strip())
    return bool(parts.scheme or parts.netloc)


def test_dashboard_pages_exist():
    found = {p.relative_to(DASHBOARD).as_posix() for p in _dashboard_pages()}
    assert set(PAGES) <= found


@pytest.mark.parametrize(
    "page", _dashboard_pages(), ids=lambda p: p.relative_to(DASHBOARD).as_posix()
)
def test_no_dashboard_page_loads_a_cross_origin_script(page):
    external = [s for s in _script_srcs(page.read_text(encoding="utf-8")) if _is_cross_origin(s)]
    assert external == [], (
        f"{page.relative_to(REPO)} loads scripts from another origin: {external}. "
        "Vendor them under dashboard/redesign/vendor/ and pin them here."
    )


@pytest.mark.parametrize("rel", sorted(VENDORED_SHA512))
def test_vendored_file_matches_pinned_sha512(rel):
    path = VENDOR / rel
    assert path.is_file(), f"missing vendored file {path.relative_to(REPO)}"
    assert hashlib.sha512(path.read_bytes()).hexdigest() == VENDORED_SHA512[rel]


def test_vendor_dir_holds_only_pinned_files():
    present = {
        p.relative_to(VENDOR).as_posix()
        for p in VENDOR.rglob("*")
        if p.is_file() and p.name != "README.md"
    }
    assert present == set(VENDORED_SHA512)


@pytest.mark.parametrize("rel", sorted(LICENCE_HEADERS))
def test_vendored_script_keeps_licence_header(rel):
    assert (VENDOR / rel).read_text(encoding="utf-8").startswith(LICENCE_HEADERS[rel])


def _vendored_refs(page_rel: str, page_url: str) -> set[str]:
    """Vendored paths (relative to vendor/) a page's script tags resolve to."""
    html = (DASHBOARD / page_rel).read_text(encoding="utf-8")
    # app.html is served with <base href="/dashboard/redesign/">.
    base = "/dashboard/redesign/" if page_rel.startswith("redesign/") else page_url
    prefix = "/dashboard/redesign/vendor/"
    refs = set()
    for src in _script_srcs(html):
        path = urlsplit(urljoin(base, src)).path
        if path.startswith(prefix):
            refs.add(path[len(prefix):])
    return refs


@pytest.mark.parametrize("page_rel", sorted(PAGES))
def test_page_loads_its_vendored_scripts(page_rel):
    page_url, expected = PAGES[page_rel]
    assert _vendored_refs(page_rel, page_url) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("page_rel", sorted(PAGES))
async def test_vendored_scripts_are_served_unchanged(page_rel):
    page_url, _ = PAGES[page_rel]
    for rel in _vendored_refs(page_rel, page_url):
        resp = await http_dashboard_redesign(
            SimpleNamespace(path_params={"file": f"vendor/{rel}"})
        )
        assert resp.status_code == 200, (rel, resp.body[:200])
        assert resp.media_type == "application/javascript"
        assert resp.body == (VENDOR / rel).read_bytes()


@pytest.mark.asyncio
async def test_served_entry_page_has_no_cross_origin_script():
    resp = await http_dashboard_redesign(SimpleNamespace(path_params={"file": ""}))
    assert resp.status_code == 200
    srcs = _script_srcs(resp.body.decode("utf-8"))
    assert [s for s in srcs if _is_cross_origin(s)] == []
    assert any(s.startswith("./vendor/chart.js-4.4.0/chart.umd.js") for s in srcs)
