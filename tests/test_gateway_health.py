"""The gateway must serve /health — deploy tooling gates on it.

deploy-gateway.sh curls /health to decide OK vs FAILED, and deploy-status.sh
shows it as the service's health column. Before this route existed every
gateway deploy reported a false FAILED against a healthy process (three times
on 2026-08-01/02 alone).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gateway_server  # noqa: E402


def test_health_route_is_registered():
    app = gateway_server.mcp.streamable_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/health" in paths, f"gateway serves no /health; routes: {paths}"


def test_health_route_answers_ok():
    from starlette.testclient import TestClient

    app = gateway_server.mcp.streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
