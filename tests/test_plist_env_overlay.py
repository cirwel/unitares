"""The deployment environment overlay that deploy-mcp.sh applies to the live
governance plist (scripts/ops/apply_plist_env_overlay.py)."""

from __future__ import annotations

import importlib.util
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/ops/apply_plist_env_overlay.py"
OVERLAY = REPO / "scripts/ops/governance-mcp.env"
DEPLOY = REPO / "scripts/ops/deploy-mcp.sh"

_spec = importlib.util.spec_from_file_location("apply_plist_env_overlay", SCRIPT)
overlay_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(overlay_mod)


def _plist(tmp_path: Path, env: dict[str, str] | None, mode: int = 0o600) -> Path:
    path = tmp_path / "com.unitares.governance-mcp.plist"
    payload = {"Label": "com.unitares.governance-mcp", "ProgramArguments": ["python3"]}
    if env is not None:
        payload["EnvironmentVariables"] = env
    path.write_bytes(plistlib.dumps(payload))
    path.chmod(mode)
    return path


def _overlay(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "governance-mcp.env"
    path.write_text(text)
    return path


def _env(plist: Path) -> dict[str, str]:
    return plistlib.loads(plist.read_bytes())["EnvironmentVariables"]


def test_sets_missing_and_changed_keys_and_leaves_the_rest(tmp_path):
    plist = _plist(tmp_path, {"UNITARES_HTTP_API_TOKEN": "secret", "A_KEY": "old"})
    overlay = _overlay(tmp_path, "# comment\n\nA_KEY=new\nB_KEY=100.64.0.0/10\n")
    assert overlay_mod.apply_overlay(plist, overlay) == ["A_KEY", "B_KEY"]
    assert _env(plist) == {"UNITARES_HTTP_API_TOKEN": "secret", "A_KEY": "new",
                           "B_KEY": "100.64.0.0/10"}


def test_an_applied_overlay_is_a_no_op_and_does_not_rewrite(tmp_path):
    # An unchanged plist keeps its hash, so the deploy kickstarts instead of
    # reloading (deploy-lib.sh's plist-hash sidecar).
    plist = _plist(tmp_path, {"A_KEY": "v"})
    overlay = _overlay(tmp_path, "A_KEY=v\n")
    before = plist.read_bytes()
    assert overlay_mod.apply_overlay(plist, overlay) == []
    assert plist.read_bytes() == before


def test_creates_the_environment_dict_and_keeps_the_mode(tmp_path):
    plist = _plist(tmp_path, None, mode=0o600)
    overlay_mod.apply_overlay(plist, _overlay(tmp_path, "A_KEY=v\n"))
    assert _env(plist) == {"A_KEY": "v"}
    assert stat.S_IMODE(plist.stat().st_mode) == 0o600


def test_dry_run_reports_without_writing(tmp_path):
    plist = _plist(tmp_path, {})
    before = plist.read_bytes()
    assert overlay_mod.apply_overlay(plist, _overlay(tmp_path, "A_KEY=v\n"), dry_run=True) == ["A_KEY"]
    assert plist.read_bytes() == before


@pytest.mark.parametrize("text", ["no equals sign\n", "lower=1\n", "A_KEY=1\nA_KEY=2\n", "=v\n"])
def test_a_malformed_overlay_is_refused_and_the_plist_untouched(tmp_path, text):
    plist = _plist(tmp_path, {"A_KEY": "old"})
    before = plist.read_bytes()
    result = subprocess.run([sys.executable, str(SCRIPT), "--plist", str(plist),
                             "--overlay", str(_overlay(tmp_path, text))],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert plist.read_bytes() == before


def test_values_are_never_printed(tmp_path):
    plist = _plist(tmp_path, {})
    result = subprocess.run([sys.executable, str(SCRIPT), "--plist", str(plist),
                             "--overlay", str(_overlay(tmp_path, "A_KEY=sekrit-value\n"))],
                            capture_output=True, text=True, check=True)
    assert "A_KEY" in result.stdout and "sekrit-value" not in result.stdout + result.stderr


def test_the_tracked_overlay_parses_and_the_deploy_applies_it():
    values = overlay_mod.parse_overlay(OVERLAY.read_text())
    assert values, "the deployment overlay must set something"
    deploy = DEPLOY.read_text()
    apply_at = deploy.index("apply_plist_env_overlay.py")
    assert apply_at < deploy.index('deploy_lib_restart_service "$TAG"'), \
        "the overlay must land before the restart that reloads a changed plist"


def test_the_overlay_is_second_family_reviewed():
    # It can widen the auth bypass (UNITARES_TRUSTED_NETWORKS), like access.py.
    import json
    paths = json.loads((REPO / "scripts/dev/review_policy.json").read_text())["second_family_paths"]
    assert "scripts/ops/governance-mcp.env" in paths
