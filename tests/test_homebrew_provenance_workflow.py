"""Exercise source release refusal paths without signing or publishing anything."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/homebrew-release-sync.yml').read_text())
STEPS = WORKFLOW['jobs']['source_archive']['steps']
COMMIT = 'a' * 40


def step(prefix):
    return next(s for s in STEPS if s.get('name', '').startswith(prefix))


@pytest.fixture
def release_env(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    git = bin_dir / 'git'
    git.write_text('''#!/bin/bash
case "$1" in
  rev-parse|rev-list) printf '%s\\n' "$EXPECTED_COMMIT" ;;
  archive) printf 'deterministic test archive' ;;
  *) exit 98 ;;
esac
''')
    gh = bin_dir / 'gh'
    gh.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
case "$1 $2" in
  'attestation verify')
    if [ "$BAD_SIGNATURE" = true ]; then exit 1; fi
    printf '%s\\n' "$VERIFICATIONS" ;;
  'release download') exit 1 ;;
  'release upload') exit 0 ;;
  *) exit 99 ;;
esac
''')
    git.chmod(0o755)
    gh.chmod(0o755)
    return {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
            'TAG': 'v3.7.0', 'GITHUB_REF': 'refs/tags/v3.7.0',
            'GITHUB_SHA': COMMIT, 'EXPECTED_COMMIT': COMMIT,
            'RUNNER_TEMP': str(tmp_path), 'GITHUB_ENV': str(tmp_path / 'env'),
            'GITHUB_OUTPUT': str(tmp_path / 'outputs'),
            'GITHUB_REPOSITORY': 'cirwel/unitares', 'CALLS': str(tmp_path / 'calls'),
            'UNITARES_VERSION': '3.7.0', 'UNITARES_COMMIT': COMMIT,
            'BAD_SIGNATURE': 'false', 'VERIFICATIONS': '[{"verificationResult":{}}]'}


def run_step(prefix, env):
    return subprocess.run(['bash', '-c', step(prefix)['run']], env=env,
                          text=True, capture_output=True)


@pytest.mark.parametrize('field,value', [('GITHUB_REF', 'refs/heads/master'),
                                        ('GITHUB_SHA', 'b' * 40), ('TAG', '../bad')])
def test_checkout_cannot_impersonate_release_context(release_env, field, value):
    release_env[field] = value
    result = run_step('Build or verify', release_env)
    assert result.returncode != 0
    assert not Path(release_env['CALLS']).exists()


def test_build_never_publishes_unsigned_archive(release_env):
    assert run_step('Build or verify', release_env).returncode == 0
    assert 'release upload' not in Path(release_env['CALLS']).read_text()
    assert step('Attest')['uses'].endswith('4d101475d8b20a2381f78447822ac1eab6504dd8')
    assert STEPS.index(step('Attest')) < STEPS.index(step('Verify provenance'))


@pytest.mark.parametrize('setting,value', [('BAD_SIGNATURE', 'true'), ('VERIFICATIONS', '[]')])
def test_invalid_or_empty_verification_blocks_publication(release_env, setting, value):
    release_env[setting] = value
    result = run_step('Verify provenance', release_env)
    assert result.returncode != 0
    calls = Path(release_env['CALLS']).read_text()
    assert 'release upload' not in calls
    assert 'release download' not in calls


def test_publisher_uses_fixed_signer_and_exact_source(release_env):
    result = run_step('Verify provenance', release_env)
    assert result.returncode == 0, result.stderr
    calls = Path(release_env['CALLS']).read_text()
    assert '--repo cirwel/unitares' in calls
    assert '--signer-workflow cirwel/unitares/.github/workflows/homebrew-release-sync.yml' in calls
    assert f'--source-digest {COMMIT}' in calls
    assert f'--signer-digest {COMMIT}' in calls
    assert '--source-ref refs/tags/v3.7.0' in calls
    assert '--deny-self-hosted-runners' in calls
    assert calls.index('attestation verify') < calls.index('release upload')


def test_tap_code_cannot_inherit_upstream_signing_or_release_write_authority():
    signing = WORKFLOW['jobs']['source_archive']
    rendering = WORKFLOW['jobs']['render']
    assert signing['permissions'] == {'contents': 'write', 'id-token': 'write', 'attestations': 'write'}
    assert rendering['permissions'] == {'contents': 'read', 'pull-requests': 'read'}
    assert rendering['needs'] == 'source_archive'
    assert 'environment' not in rendering
    guard = rendering['steps'][0]
    assert guard['env']['APP_KEY_PRESENT'] == "${{ secrets.UNITARES_AGENT_APP_PRIVATE_KEY != '' }}"
    assert guard['run'] == 'test "$APP_KEY_PRESENT" = false'
    assert all('create-github-app-token' not in s.get('uses', '') for s in rendering['steps'])
    publishing = WORKFLOW['jobs']['sync']
    assert publishing['needs'] == ['source_archive', 'render']
    assert publishing['permissions'] == {'contents': 'read', 'actions': 'read'}
    assert all('python' not in s.get('run', '') for s in publishing['steps'])
    assert all('_tap' not in s.get('working-directory', '') for s in signing['steps'])
    assert not any(s.get('with', {}).get('repository') == 'cirwel/homebrew-unitares' for s in signing['steps'])
    assert set(signing['outputs']) == {'version', 'commit', 'sha256', 'url'}
    assert rendering['env']['UNITARES_COMMIT'] == '${{ needs.source_archive.outputs.commit }}'


def test_archive_outputs_match_the_verified_source(release_env):
    result = run_step('Build or verify', release_env)
    assert result.returncode == 0, result.stderr
    outputs = dict(line.split('=', 1) for line in Path(release_env['GITHUB_OUTPUT']).read_text().splitlines())
    assert outputs['version'] == '3.7.0'
    assert outputs['commit'] == COMMIT
    assert len(outputs['sha256']) == 64
    assert outputs['url'] == 'https://github.com/cirwel/unitares/releases/download/v3.7.0/unitares-3.7.0.tar.gz'


def test_render_guard_refuses_a_repository_level_app_key(release_env):
    guard = WORKFLOW['jobs']['render']['steps'][0]['run']
    for present, expected in [('true', 1), ('false', 0)]:
        result = subprocess.run(['bash', '-c', guard], env={**release_env, 'APP_KEY_PRESENT': present})
        assert result.returncode == expected


@pytest.mark.parametrize('path,mode,accepted', [
    ('Formula/unitares.rb', '100644', True),
    ('SOURCES.json', '100644', True),
    ('Formula/unitares.rb', '120000', False),
    ('scripts/release_sync.py', '100644', False),
])
def test_publisher_restricts_patch_paths_and_modes(tmp_path, path, mode, accepted):
    publishing = WORKFLOW['jobs']['sync']['steps'][-1]['run']
    end = 'done < <(git diff --cached --name-only)'
    policy = publishing[publishing.index('while IFS= read -r path; do'):publishing.index(end) + len(end)]
    binary = tmp_path / 'git'
    binary.write_text('#!/bin/bash\ncase "$1" in\n  diff) printf "%s\\n" "$TEST_PATH" ;;\n  ls-files) printf "%s abc 0\\t%s\\n" "$TEST_MODE" "$TEST_PATH" ;;\nesac\n')
    binary.chmod(0o755)
    result = subprocess.run(['bash', '-euc', policy], env={**os.environ,
        'PATH': f'{tmp_path}:{os.environ["PATH"]}', 'TEST_PATH': path, 'TEST_MODE': mode}, capture_output=True)
    assert (result.returncode == 0) == accepted
