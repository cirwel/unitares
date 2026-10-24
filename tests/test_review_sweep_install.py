"""The reusable review entrypoint dispatches a scheduler-driven sweep."""
import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/dev/review.sh"


def test_review_entrypoint_dispatches_sweep_without_starting_author_review(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    python.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    entrypoint = SCRIPT
    result = subprocess.run(["bash", str(entrypoint), "sweep", "--worktree", str(tmp_path), "--dry-run"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["scripts/dev/review_gate.py", "sweep", "--worktree", str(tmp_path), "--dry-run"]
