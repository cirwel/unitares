"""Operator dashboard extensions (/dashboard/ext/*).

The core dashboard ships only sections every install can read. A deployment's
own panels live outside the repo, in UNITARES_DASHBOARD_EXT_DIR, and this route
serves them. The properties that matter to an outside operator: unset means
nothing is served, and set means only an authenticated caller gets the code.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_dashboard_ext

TRUSTED = ("127.0.0.1", 50000)
ANON = ("203.0.113.7", 44444)


@pytest.fixture(autouse=True)
def _local_posture(monkeypatch):
    # Same posture as the snapshot gate tests: no bearer configured, so a
    # loopback peer is trusted and a public peer is not.
    for key in ("UNITARES_HTTP_API_TOKEN", "UNITARES_MCP_BEARER_TOKENS", "UNITARES_REST_STRICT"):
        monkeypatch.delenv(key, raising=False)


def _client(peer):
    app = Starlette(routes=[Route("/dashboard/ext/{file:path}", http_dashboard_ext, methods=["GET"])])
    return TestClient(app, client=peer)


@pytest.fixture
def ext_dir(tmp_path, monkeypatch):
    (tmp_path / "sections").mkdir()
    (tmp_path / "manifest.json").write_text(json.dumps({"sections": [{"id": "queue", "global": "Queue", "script": "sections/queue.js"}]}))
    (tmp_path / "sections" / "queue.js").write_text("window.Queue = { load() {} };")
    (tmp_path / "notes.md").write_text("private")
    (tmp_path.parent / "outside.js").write_text("secret")
    monkeypatch.setenv("UNITARES_DASHBOARD_EXT_DIR", str(tmp_path))
    return tmp_path


def test_unset_serves_nothing(monkeypatch):
    """The default install: no extensions, even for a trusted caller."""
    monkeypatch.delenv("UNITARES_DASHBOARD_EXT_DIR", raising=False)
    r = _client(TRUSTED).get("/dashboard/ext/manifest.json")
    assert r.status_code == 404


def test_manifest_and_scripts_served_to_trusted_caller(ext_dir):
    r = _client(TRUSTED).get("/dashboard/ext/manifest.json")
    assert r.status_code == 200
    assert r.json()["sections"][0]["id"] == "queue"
    js = _client(TRUSTED).get("/dashboard/ext/sections/queue.js")
    assert js.status_code == 200
    assert js.headers["content-type"].startswith("application/javascript")
    assert "Queue" in js.text
    assert js.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["manifest.json", "sections/queue.js"])
def test_extensions_are_not_public(ext_dir, path):
    """An extension is this operator's own surface; its code names what they
    measure. Anonymous callers get 401, never the file."""
    r = _client(ANON).get(f"/dashboard/ext/{path}")
    assert r.status_code == 401
    assert "Queue" not in r.text


def test_rejects_traversal_and_other_types(ext_dir):
    c = _client(TRUSTED)
    assert c.get("/dashboard/ext/../outside.js").status_code in (400, 404)
    assert c.get("/dashboard/ext/%2e%2e/outside.js").status_code in (400, 404)
    assert c.get("/dashboard/ext/notes.md").status_code == 403
    assert c.get("/dashboard/ext/missing.js").status_code == 404
