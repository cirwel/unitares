"""Opt-in route packs (src/http_routes/packs.py).

The core server mounts only routes any install can use; routes that exist
because a deployment runs the reference residents or a local automation
census mount only when UNITARES_ROUTE_PACKS names their pack. These pin the
default (nothing mounted), opt-in, and that a typo is reported rather than
silently mounting nothing.
"""

from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette

from src.http_routes import packs


def _paths(app) -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


PACK_PATHS = {
    "reference-residents": {
        "/v1/sentinel/backlog", "/v1/sentinel/summary", "/v1/sentinel/adjudication-queue",
        "/v1/sentinel/adjudicate", "/v1/sentinel/model-adjudicate",
        "/v1/watcher/summary", "/v1/vigil/summary",
    },
    "automation-census": {"/api/automations"},
}


def test_pack_table_is_what_the_docs_say():
    assert {name: {r.path for r in routes} for name, routes in packs.pack_routes().items()} == PACK_PATHS


def test_default_mounts_nothing(monkeypatch):
    monkeypatch.delenv(packs.ENV_VAR, raising=False)
    app = Starlette()
    assert packs.register_route_packs(app) == []
    assert not (_paths(app) & set().union(*PACK_PATHS.values()))


def test_named_packs_mount_only_their_routes():
    app = Starlette()
    assert packs.register_route_packs(app, raw=" automation-census ,") == ["automation-census"]
    assert _paths(app) == PACK_PATHS["automation-census"]


def test_unknown_pack_is_reported_not_silently_ignored(caplog):
    app = Starlette()
    with caplog.at_level(logging.WARNING):
        mounted = packs.register_route_packs(app, raw="reference-residents,referance-residents",
                                             logger=logging.getLogger("t"))
    assert mounted == ["reference-residents"]
    assert "unknown route pack 'referance-residents'" in caplog.text


def test_duplicates_are_mounted_once():
    app = Starlette()
    packs.register_route_packs(app, raw="automation-census,automation-census")
    assert [r.path for r in app.routes].count("/api/automations") == 1


@pytest.mark.parametrize("enabled", ["", "reference-residents,automation-census"])
def test_production_registrar_honours_the_env(monkeypatch, enabled):
    """Through register_http_routes, the function the server actually calls."""
    from src.http_api import register_http_routes

    if enabled:
        monkeypatch.setenv(packs.ENV_VAR, enabled)
    else:
        monkeypatch.delenv(packs.ENV_VAR, raising=False)
    app = Starlette()
    register_http_routes(app, server_ready_fn=lambda: True, server_start_time=0.0,
                         server_version="t", has_streamable_http=False)
    mounted = _paths(app) & set().union(*PACK_PATHS.values())
    assert mounted == (set().union(*PACK_PATHS.values()) if enabled else set())
    assert "/v1/residents" in _paths(app)  # core stays regardless
