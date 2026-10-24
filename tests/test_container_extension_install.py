"""Exercise Docker's shipped source set without optional residents or a workspace."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def artifact(tmp_path_factory):
    target = tmp_path_factory.mktemp('image-source')
    # Use the Dockerfile's actual COPY declarations so a packaging change changes
    # the tested artifact. Dependencies use the existing interpreter environment.
    for line in (ROOT / 'Dockerfile').read_text().splitlines():
        if not line.startswith('COPY ') or ' --from=' in line:
            continue
        fields = line.split()[1:]
        if len(fields) != 2 or fields[0].startswith('--'):
            continue
        source, dest = fields
        if not (ROOT / source).exists():
            continue
        if (ROOT / source).is_dir():
            shutil.copytree(ROOT / source, target / dest, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', '*.egg-info', '.venv'))
        else:
            dst = target / (Path(source).name if dest == '.' else dest)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / source, dst)
    return target


def run_artifact(artifact, tmp_path, script, **settings):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('UNITARES_', 'VIGIL_', 'CHRONICLER_', 'GITHUB_', 'PG', 'DATABASE_', 'LEASE_PLANE_'))}
    env.update(HOME=str(tmp_path), PYTHONPATH=os.pathsep.join([str(artifact), str(artifact / 'agents/sdk/src')]))
    env.update(settings)
    result = subprocess.run([sys.executable, '-c', script], cwd=artifact, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_shipped_artifact_supports_core_reviewer_and_shared_helpers(artifact, tmp_path):
    assert not (artifact / 'agents/vigil').exists()
    assert not (artifact / 'agents/chronicler').exists()
    run_artifact(artifact, tmp_path, '''
from pathlib import Path
from starlette.applications import Starlette
from src.http_api import register_http_routes
import src, unitares_sdk, agents.common.resolution_outcome, agents.dialectic_reviewer.reviewer
assert Path(src.__file__).resolve().is_relative_to(Path.cwd())
assert Path(unitares_sdk.__file__).resolve().is_relative_to(Path.cwd())
app = Starlette()
register_http_routes(app, server_ready_fn=lambda: True, server_start_time=0.0, server_version="test", has_streamable_http=False)
assert "/v1/residents" in {r.path for r in app.routes}
assert not any(r.path.startswith(("/v1/vigil", "/v1/watcher", "/v1/sentinel")) for r in app.routes)
''')


def test_external_extension_and_catalog_work_in_shipped_artifact(artifact, tmp_path):
    handler = tmp_path / 'external.py'
    handler.write_text('from starlette.responses import JSONResponse\nasync def summary(request):\n    return JSONResponse({"extension": "installed"})\n')
    manifest = tmp_path / 'packs.json'
    manifest.write_text(json.dumps({'schema': 'unitares.route-packs.v1', 'packs': {'summary': [
        {'path': '/extensions/summary', 'handler': str(handler) + ':summary', 'methods': ['GET']} ]}}))
    catalog = tmp_path / 'metrics.json'
    catalog.write_text(json.dumps({'metrics': [{'name': 'example.jobs.count', 'description': 'Completed example jobs', 'unit': 'count'}]}))
    run_artifact(artifact, tmp_path, '''
from starlette.applications import Starlette
from starlette.testclient import TestClient
from src.http_api import register_http_routes
from src.fleet_metrics.catalog import catalog
assert "example.jobs.count" in catalog
app = Starlette()
register_http_routes(app, server_ready_fn=lambda: True, server_start_time=0.0, server_version="test", has_streamable_http=False)
with TestClient(app) as client:
    assert client.get("/extensions/summary").status_code == 401
    response = client.get("/extensions/summary", headers={"Authorization": "Bearer fixture"})
    assert response.status_code == 200 and response.json() == {"extension": "installed"}
''', UNITARES_ROUTE_PACKS='summary', UNITARES_ROUTE_PACKS_FILE=str(manifest),
       UNITARES_METRICS_CATALOG_EXTRA=str(catalog), UNITARES_REST_STRICT='1', UNITARES_MCP_BEARER_TOKENS='fixture')
