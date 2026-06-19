"""Covers the additive /dashboard/redesign preview route.

The redesign reference is served from dashboard/redesign/** so it renders on
live governance data without touching the production /dashboard route. These
tests exercise the handler directly: it serves the entry page and nested
assets, and it stays sandboxed (no traversal, no disallowed file types).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.http_api import http_dashboard_redesign


def _req(file: str):
    return SimpleNamespace(path_params={"file": file})


@pytest.mark.asyncio
async def test_serves_entry_page_by_default():
    resp = await http_dashboard_redesign(_req(""))
    assert resp.status_code == 200
    assert resp.media_type == "text/html"
    assert b"UNITARES" in resp.body


@pytest.mark.asyncio
async def test_serves_nested_section_module():
    resp = await http_dashboard_redesign(_req("sections/landing.js"))
    assert resp.status_code == 200
    assert resp.media_type == "application/javascript"
    assert b"Landing" in resp.body


@pytest.mark.asyncio
async def test_serves_tokens_css():
    resp = await http_dashboard_redesign(_req("tokens.css"))
    assert resp.status_code == 200
    assert resp.media_type == "text/css"


@pytest.mark.asyncio
async def test_rejects_path_traversal():
    resp = await http_dashboard_redesign(_req("../http_api.py"))
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rejects_disallowed_extension():
    resp = await http_dashboard_redesign(_req("shot-eisv.png"))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_missing_file_is_404():
    resp = await http_dashboard_redesign(_req("does-not-exist.js"))
    assert resp.status_code == 404
