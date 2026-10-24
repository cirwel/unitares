"""Generic repository audits must never silently query a maintainer account."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('script', ['stuck_draft_audit.py', 'stranded_work_audit.py'])
def test_missing_repository_refuses_before_remote_lookup(script, tmp_path):
    path = Path(__file__).resolve().parents[1] / 'scripts/dev' / script
    result = subprocess.run([sys.executable, str(path)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert '--repo' in result.stderr and 'required' in result.stderr
