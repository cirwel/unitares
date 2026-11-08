"""Exercise source release refusal paths without signing or publishing anything."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/homebrew-release-sync.yml').read_text())
STEPS = WORKFLOW['jobs']['sync']['steps']
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
