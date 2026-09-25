"""The production default paths of the writers conftest redirects.

tests/conftest.py (``_isolate_repo_data_writers``) patches these module
constants and exports their env overrides for the whole session, so an
in-process assertion would compare the tmp value with itself. Read them in
a fresh interpreter with the overrides removed instead.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

OVERRIDES = (
    "UNITARES_AUDIT_LOG",
    "UNITARES_PROCESS_DIR",
    "UNITARES_LOCK_DIR",
    "UNITARES_CALIBRATION_STATE",
    "UNITARES_SEQUENTIAL_CALIBRATION_STATE",
)

PROBE = """
import json
import src.audit_log, src.process_cleanup, src.state_locking
import src.calibration, src.sequential_calibration
print(json.dumps({
    "audit": str(src.audit_log.DEFAULT_LOG_FILE),
    "pids": str(src.process_cleanup.DEFAULT_PID_DIR),
    "locks": str(src.state_locking.DEFAULT_LOCK_DIR),
    "calibration": str(src.calibration.DEFAULT_STATE_FILE),
    "sequential": str(src.sequential_calibration.DEFAULT_STATE_FILE),
}))
"""


def _defaults(env):
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_unset_overrides_resolve_to_the_checkouts_data_dir():
    env = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
    data = REPO_ROOT / "data"
    assert _defaults(env) == {
        "audit": str(data / "audit_log.jsonl"),
        "pids": str(data / "processes"),
        "locks": str(data / "locks"),
        "calibration": str(data / "calibration_state.json"),
        "sequential": str(data / "sequential_calibration_state.json"),
    }


def test_overrides_replace_each_default(tmp_path):
    env = dict(os.environ)
    env.update({name: str(tmp_path / name) for name in OVERRIDES})
    got = _defaults(env)
    assert got == {
        "audit": str(tmp_path / "UNITARES_AUDIT_LOG"),
        "pids": str(tmp_path / "UNITARES_PROCESS_DIR"),
        "locks": str(tmp_path / "UNITARES_LOCK_DIR"),
        "calibration": str(tmp_path / "UNITARES_CALIBRATION_STATE"),
        "sequential": str(tmp_path / "UNITARES_SEQUENTIAL_CALIBRATION_STATE"),
    }
