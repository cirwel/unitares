"""Release scaffolding must preserve work if generation fails."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "prepare_interface_release", Path(__file__).parents[1] / "scripts/dev/prepare_interface_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for p in ("src", "docs/changelog.d"):
        (tmp_path / p).mkdir(parents=True)
    (tmp_path / "src/interface_contract.py").write_text('INTERFACE_CONTRACT_VERSION = "1.33.0"\n')
    (tmp_path / "docs/INTERFACE_CONTRACT.md").write_text('Current version `1.33.0`\n')
    (tmp_path / "docs/interface-contract.v1.json").write_text('{"version":"1.33.0"}\n')
    monkeypatch.setattr(release, "affected_skills", lambda root, sources: {"knowledge-graph": sorted(sources)})
    return tmp_path


def run(root, **kwargs):
    return release.prepare(root, "1.34.0", "Bound imported previews.", "preview", "2026-10-13",
                           {"src/mcp_handlers/middleware/envelope_step.py"}, **kwargs)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_dry_run_is_nonmutating_and_lists_all_destinations(repo):
    before = snapshot(repo)
    result = run(repo, dry_run=True)
    assert snapshot(repo) == before
    assert len(result['files']) == 4
    assert 'src/interface_contract.py' in result['skills_to_recheck']['knowledge-graph']


def test_generation_writes_consistent_version_and_dated_note(repo, monkeypatch):
    def generate(*args, **kwargs):
        assert '1.34.0 (2026-10-13)' in (repo / 'src/interface_contract.py').read_text()
        return b'{"version":"1.34.0"}\n'
    monkeypatch.setattr(release.subprocess, 'check_output', generate)
    run(repo)
    assert json.loads((repo / 'docs/interface-contract.v1.json').read_text())['version'] == '1.34.0'
    assert '## 1.34.0 (2026-10-13)' in (repo / 'docs/INTERFACE_CONTRACT.md').read_text()
    assert (repo / 'docs/changelog.d/changed-preview.md').exists()


@pytest.mark.parametrize('output', [b'not json', b'{"version":"1.33.0"}'])
def test_failed_generation_restores_existing_edits(repo, monkeypatch, output):
    before = snapshot(repo)
    monkeypatch.setattr(release.subprocess, 'check_output', lambda *a, **kw: output)
    with pytest.raises(ValueError):
        run(repo)
    assert snapshot(repo) == before


def test_refuses_existing_fragment_without_writes(repo):
    (repo / 'docs/changelog.d/changed-preview.md').write_text('existing work\n')
    before = snapshot(repo)
    with pytest.raises(ValueError, match='overwrite'):
        run(repo)
    assert snapshot(repo) == before


@pytest.mark.parametrize('version', ['1.33.0', '1.2.0', 'bogus'])
def test_refuses_nonadvancing_or_invalid_version(repo, version):
    before = snapshot(repo)
    with pytest.raises(ValueError):
        release.prepare(repo, version, 'Summary', 'slug', '2026-10-13', set())
    assert snapshot(repo) == before


def test_real_sidecar_sources_are_discovered():
    sources = {'src/mcp_handlers/middleware/envelope_step.py'}
    impacted = release.affected_skills(release.ROOT, sources)
    assert 'knowledge-graph' in impacted
    assert all(paths == ['unitares/src/mcp_handlers/middleware/envelope_step.py']
               for paths in impacted.values())
