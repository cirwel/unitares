"""Buildless dashboard passkey UI contract."""

from pathlib import Path

import pytest

from src.dashboard_auth import _auth_page
from src.http_api import http_dashboard_redesign


ROOT = Path(__file__).parent.parent
AUTH = ROOT / "dashboard" / "redesign" / "auth"


class _Request:
    path_params = {"file": "auth/passkey.js"}


def test_auth_pages_are_installed_for_server_handlers():
    for page in ("signin.html", "enroll.html"):
        response = _auth_page(page)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert b"PasskeyUI" in response.body


@pytest.mark.asyncio
async def test_redesign_static_route_serves_shared_passkey_module():
    response = await http_dashboard_redesign(_Request())
    assert response.status_code == 200
    assert response.media_type == "application/javascript"
    assert b"X-Unitares-Enroll-Code" in response.body


@pytest.mark.asyncio
async def test_auth_html_cannot_bypass_gated_auth_routes():
    request = _Request()
    request.path_params = {"file": "auth/enroll.html"}
    response = await http_dashboard_redesign(request)
    assert response.status_code == 404


def test_typed_enrollment_code_never_enters_a_url():
    sources = "\n".join(
        (AUTH / name).read_text()
        for name in ("signin.html", "enroll.html", "passkey.js")
    )
    assert "?code=" not in sources
    assert 'fetch("/auth/enroll", {' in sources
    assert '"X-Unitares-Enroll-Code": normalized' in sources
    assert 'sessionStorage.setItem(ENROLL_STORAGE_KEY, normalized)' in sources


def test_security_section_is_wired_through_data_layer():
    app = (ROOT / "dashboard" / "redesign" / "app.html").read_text()
    section = (ROOT / "dashboard" / "redesign" / "sections" / "security.js").read_text()
    assert 'data-section="security"' in app
    assert 'data-pane="security"' in app
    assert "window.DATA.passkeySecurity()" in section
    assert "fetch(" not in section
