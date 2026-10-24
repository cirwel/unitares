"""Runtime defaults are checked outside setuptools, including configuration."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('operator_defaults', ROOT / 'scripts/dev/check_operator_defaults.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize('relative,source', [
    ('agents/example/agent.py', 'ROOT = "/Users/cirwel/projects/example"'),
    ('agents/example/config.json', '{"metric": "github.cirwel.traffic.views"}'),
    ('scripts/ops/job.sh', 'ROOT="${ROOT:-$HOME/projects/unitares}"'),
    ('scripts/ops/job.py', 'REPO = os.getenv("REPO", "CIRWEL/unitares")'),
    ('elixir/example/config.exs', 'config :example, url: "https://gov.cirwel.org"'),
    ('scripts/ops/job.plist.template', '<string>http://lumen:8766</string>'),
])
def test_detects_defaults_in_all_runtime_surfaces(tmp_path, relative, source):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(source)
    assert guard.scan_file(path, relative)


@pytest.mark.parametrize('relative,source', [
    ('agents/example/agent.py', '"""Measured on gov.cirwel.org in 2025."""\nROOT = os.getenv("ROOT")'),
    ('scripts/ops/job.sh', '# Old install used $HOME/projects/unitares\nROOT="${ROOT:?set ROOT}"'),
    ('scripts/ops/job.plist.template', '<!-- once http://lumen:8766 -->\n<string>__SERVICE_URL__</string>'),
    ('elixir/example/mix.exs', 'links: %{source: "https://github.com/cirwel/unitares"}'),
])
def test_preserves_configured_integrations_and_provenance(tmp_path, relative, source):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(source)
    assert not guard.scan_file(path, relative)


def test_inventory_includes_new_runtime_files_but_not_test_fixtures(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    files = ['agents/example/agent.py', 'scripts/ops/job.sh', 'elixir/example/config.exs',
             'agents/example/tests/test_agent.py', 'scripts/ops/test-job.sh']
    for relative in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')
    assert set(guard.runtime_files(tmp_path)) == set(files[:3])
