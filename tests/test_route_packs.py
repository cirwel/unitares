"""Explicit external packs retain the server credential gate on every method."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.routing import Route

from src.http_routes import packs


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    code = tmp_path / 'summary.py'
    code.write_text('''from dataclasses import dataclass
from starlette.responses import JSONResponse
@dataclass
class Summary:
    value: str = "external"
async def summary(request):
    return JSONResponse({"value": Summary().value, "method": request.method})
''')
    path = tmp_path / 'packs.json'
    path.write_text(json.dumps({'schema': 'unitares.route-packs.v1', 'packs': {
        'example-summary': [{'path': '/extensions/summary', 'handler': 'summary.py:summary',
                             'methods': ['GET', 'POST']}],
        'other': [{'path': '/extensions/other', 'handler': 'summary.py:summary', 'methods': ['GET']}],
    }}))
    monkeypatch.setenv(packs.MANIFEST_ENV, str(path))
    return path


def test_default_mounts_and_imports_nothing(monkeypatch):
    monkeypatch.delenv(packs.ENV_VAR, raising=False)
    monkeypatch.setenv(packs.MANIFEST_ENV, '/unavailable/manifest.json')
    monkeypatch.setattr(packs, 'load_pack_specs', lambda: pytest.fail('disabled manifest read'))
    app = Starlette()
    assert packs.register_route_packs(app) == []
    assert not app.routes


def test_selected_pack_only_and_duplicate_selection(manifest):
    app = Starlette()
    assert packs.register_route_packs(app, raw=' example-summary,example-summary ') == ['example-summary']
    assert [r.path for r in app.routes] == ['/extensions/summary']


def test_unknown_pack_reported(manifest, caplog):
    app = Starlette()
    with caplog.at_level(logging.WARNING):
        assert packs.register_route_packs(app, raw='typo', logger=logging.getLogger('t')) == []
    assert "unknown route pack 'typo'" in caplog.text


@pytest.mark.parametrize('enabled', ['', 'example-summary'])
def test_production_registrar_honours_env(manifest, monkeypatch, enabled):
    from src.http_api import register_http_routes
    monkeypatch.setenv(packs.ENV_VAR, enabled)
    app = Starlette()
    register_http_routes(app, server_ready_fn=lambda: True, server_start_time=0.0,
                         server_version='t', has_streamable_http=False)
    paths = {r.path for r in app.routes}
    assert ('/extensions/summary' in paths) == bool(enabled)
    assert '/v1/residents' in paths


@pytest.mark.parametrize('method', ['GET', 'POST', 'HEAD'])
@pytest.mark.parametrize('strict', ['0', '1'])
def test_external_code_cannot_skip_existing_auth_gate(manifest, monkeypatch, method, strict):
    monkeypatch.setenv('UNITARES_REST_STRICT', strict)
    monkeypatch.setenv('UNITARES_HTTP_API_TOKEN', 'local-fixture')
    monkeypatch.setenv('UNITARES_MCP_BEARER_TOKENS', 'hosted-fixture')
    app = Starlette()
    packs.register_route_packs(app, raw='example-summary')
    with TestClient(app) as client:
        assert client.request(method, '/extensions/summary').status_code == 401
        assert client.request(method, '/extensions/summary', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        token = 'hosted-fixture' if strict == '1' else 'local-fixture'
        response = client.request(method, '/extensions/summary', headers={'Authorization': f'Bearer {token}'})
        assert response.status_code == 200
        if method != 'HEAD':
            assert response.json() == {'value': 'external', 'method': method}


@pytest.mark.parametrize('invalid', ['schema', 'shape', 'methods', 'reference', 'missing_file'])
def test_invalid_manifest_mounts_nothing(manifest, invalid, caplog):
    doc = json.loads(manifest.read_text())
    route = doc['packs']['example-summary'][0]
    if invalid == 'schema':
        doc['schema'] = 'unknown'
    elif invalid == 'shape':
        route['unexpected'] = True
    elif invalid == 'methods':
        route['methods'] = ['TRACE']
    elif invalid == 'reference':
        route['handler'] = 'invalid'
    else:
        manifest.unlink()
    if invalid != 'missing_file':
        manifest.write_text(json.dumps(doc))
    with caplog.at_level(logging.ERROR):
        app = Starlette()
        assert packs.register_route_packs(app, raw='example-summary', logger=logging.getLogger('t')) == []
    assert not app.routes
    assert 'manifest could not be loaded' in caplog.text


def test_pack_with_missing_handler_is_atomic(manifest):
    doc = json.loads(manifest.read_text())
    doc['packs']['example-summary'].append({'path': '/extensions/bad', 'handler': 'missing.py:nope', 'methods': ['GET']})
    manifest.write_text(json.dumps(doc))
    app = Starlette()
    assert packs.register_route_packs(app, raw='example-summary') == []
    assert not app.routes


@pytest.mark.parametrize('existing', [True, False])
def test_collisions_reject_entire_pack_including_implicit_head(manifest, existing):
    async def core(request):
        raise AssertionError('not requested')
    app = Starlette(routes=[Route('/extensions/summary', core, methods=['HEAD'])] if existing else [])
    if not existing:
        doc = json.loads(manifest.read_text())
        doc['packs']['example-summary'].append({'path': '/extensions/summary', 'handler': 'summary.py:summary', 'methods': ['HEAD']})
        manifest.write_text(json.dumps(doc))
    assert packs.register_route_packs(app, raw='example-summary') == []
    assert len(app.routes) == int(existing)


def test_compose_forwards_extension_settings():
    root = Path(__file__).resolve().parents[1]
    compose, example = (root / 'docker-compose.yml').read_text(), (root / '.env.example').read_text()
    for var in (packs.ENV_VAR, packs.MANIFEST_ENV, 'UNITARES_DASHBOARD_EXT_DIR'):
        assert f'{var}: ${{{var}:-}}' in compose
        assert f'# {var}=' in example
