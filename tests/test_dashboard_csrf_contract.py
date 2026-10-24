"""Pin the browser side of the dashboard-session CSRF contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_redesign_adds_csrf_header_to_unsafe_authenticated_fetches():
    source = (ROOT / "dashboard/redesign/data.js").read_text()

    assert 'const method = (opts.method || "GET").toUpperCase();' in source
    assert '["GET", "HEAD", "OPTIONS"].includes(method)' in source
    assert 'headers["X-Unitares-Csrf"] = "1";' in source


def test_legacy_dashboard_tool_calls_send_csrf_header():
    source = (ROOT / "dashboard/phase.js").read_text()

    assert "'X-Unitares-Csrf': '1'" in source
