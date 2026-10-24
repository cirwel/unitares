"""External BEAM names are opt-in; listing cannot attach or expose cookies."""
import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ops/beam-attach.sh'


def test_default_and_configured_beam_names(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    epmd = bin_dir / 'epmd'
    epmd.write_text('#!/bin/sh\necho "name sample_node at port 3000"\n')
    epmd.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir)+':'+os.environ['PATH'])
    env.pop('UNITARES_SECRETS_ENV', None)
    env.pop('UNITARES_BEAM_SERVICES_FILE', None)
    default = subprocess.run(['bash', str(SCRIPT), '--list'], env=env, capture_output=True, text=True, check=True)
    assert 'dispatch-beam' not in default.stdout
    assert 'orchestrator' in default.stdout
    path = tmp_path / 'services.tsv'
    path.write_text('sample\tsample_node\t')
    env['UNITARES_BEAM_SERVICES_FILE'] = str(path)
    configured = subprocess.run(['bash', str(SCRIPT), '--list'], env=env, capture_output=True, text=True, check=True)
    assert 'sample_node' in configured.stdout and 'reachable' in configured.stdout
    path.write_text('orchestrator\tother_node\t')
    invalid = subprocess.run(['bash', str(SCRIPT), '--list'], env=env, capture_output=True, text=True)
    assert invalid.returncode == 2
    assert 'duplicate BEAM service' in invalid.stderr
