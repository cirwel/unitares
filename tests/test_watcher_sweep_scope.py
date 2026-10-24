"""An unconfigured Watcher sweep must not inspect a workspace or a backend."""
import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / "agents/watcher/watcher-sweep.sh"


def test_missing_scan_roots_stops_before_state_or_network_calls(tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    calls = tmp_path / "calls"
    for name in ("mkdir", "curl", "pgrep", "touch", "python3"):
        path = bins / name
        path.write_text('#!/bin/sh\nprintf "unexpected\\n" >> "$TEST_CALL_LOG"\nexit 99\n')
        path.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "UNITARES_WATCHER_SWEEP_ROOTS"}
    env.update({"PATH": f"{bins}:{os.environ['PATH']}", "TEST_CALL_LOG": str(calls)})
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "explicit scan roots" in result.stderr
    assert not calls.exists()
