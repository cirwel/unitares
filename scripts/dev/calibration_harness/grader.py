"""External test-runner grader.

Runs a real subprocess (a python assertion script) in a sandboxed temp dir and
reads the exit code. The exit code is an honest *external signal*: the grade fed
to governance is grounded in a tool observation, not agent prose.

Returns a Grade whose ``detail`` carries the real ``exit_code``/``command`` so
the corroboration classifier reaches ``externally_verified`` (weight 1.0) on the
structured evidence too, not only on the ``external_signal`` source label.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Grade:
    is_bad: bool
    score: float           # 1.0 pass .. 0.0 fail
    exit_code: int
    detail: dict[str, Any] = field(default_factory=dict)


def grade_script(source: str, *, label: str, timeout_s: float = 15.0) -> Grade:
    """Write `source` to a temp file, run it isolated, grade by exit code.

    Sandboxing: a throwaway temp dir as cwd; the injected failures are pure
    `assert`/`sys.exit(1)` in the script text, so nothing touches real systems.
    """
    with tempfile.TemporaryDirectory(prefix="calib_ep_") as td:
        script = Path(td) / "episode.py"
        script.write_text(source)
        cmd = [sys.executable, str(script)]
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, sandboxed temp dir
                cmd,
                cwd=td,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            exit_code = proc.returncode
            stderr_tail = proc.stderr[-400:]
        except subprocess.TimeoutExpired:
            exit_code = 124
            stderr_tail = f"timeout after {timeout_s}s"

    is_bad = exit_code != 0
    detail: dict[str, Any] = {
        # Fields the corroboration classifier keys on for tool_observed:
        "kind": "test",
        "tool": "python",
        "exit_code": exit_code,
        "command": " ".join(cmd),
        "test_name": label,
        # Calibration-harness rows are controlled fixtures. They are allowed to
        # persist inside the isolated governance_test instance, but must be
        # recognizable if accidentally pointed at live governance.
        "synthetic_calibration_fixture": True,
        "do_not_use_for_live_validation": True,
        "fixture_scope": "calibration_harness",
    }
    if is_bad:
        detail["red_team_fixture"] = "calibration_harness_seeded_bad_outcome"
    if stderr_tail:
        detail["error_message"] = stderr_tail
    return Grade(is_bad=is_bad, score=0.0 if is_bad else 1.0, exit_code=exit_code, detail=detail)
