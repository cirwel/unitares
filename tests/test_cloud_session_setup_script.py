"""Contracts for the Claude cloud-session plugin bootstrap."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "dev" / "cloud-session-setup.sh"
PLUGIN_ID = "unitares-governance@unitares-governance"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
    path.chmod(0o755)


def _run_setup(
    tmp_path: Path,
    *,
    plugin_enabled: bool,
    curl_status: int = 400,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    plugin_json = json.dumps([{"id": PLUGIN_ID, "enabled": plugin_enabled}])

    _write_executable(
        fake_bin / "claude",
        """
printf '%s\\n' "$*" >> "$FAKE_COMMAND_LOG"
case "$*" in
  "plugin marketplace list") printf '%s\\n' 'unitares-governance' ;;
  "plugin list --json") printf '%s\\n' "$FAKE_PLUGIN_JSON" ;;
esac
""",
    )
    _write_executable(fake_bin / "timeout", 'shift\nexec "$@"\n')
    _write_executable(
        fake_bin / "curl",
        """
if [[ " $* " == *" -H @- "* ]]; then cat >/dev/null; fi
printf '%s' "$FAKE_CURL_STATUS"
""",
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_COMMAND_LOG": str(command_log),
        "FAKE_PLUGIN_JSON": plugin_json,
        "FAKE_CURL_STATUS": str(curl_status),
        "UNITARES_SERVER_URL": "https://gov.example.test",
        "UNITARES_HTTP_API_TOKEN": "test-token",
        "UNITARES_FILE_LEASES_ENABLED": "0",
        "UNITARES_FILE_LEASES_REQUIRED": "0",
    }
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc, command_log.read_text()


def test_disabled_plugin_is_enabled_instead_of_treated_as_active(tmp_path: Path) -> None:
    proc, commands = _run_setup(tmp_path, plugin_enabled=False)

    assert proc.returncode == 0
    assert f"plugin enable {PLUGIN_ID}" in commands
    assert f"plugin install {PLUGIN_ID}" not in commands


def test_enabled_plugin_is_left_alone(tmp_path: Path) -> None:
    proc, commands = _run_setup(tmp_path, plugin_enabled=True)

    assert proc.returncode == 0
    assert f"plugin enable {PLUGIN_ID}" not in commands
    assert f"plugin install {PLUGIN_ID}" not in commands


def test_mcp_probe_rejects_bad_bearer_even_when_health_would_be_green(
    tmp_path: Path,
) -> None:
    proc, _ = _run_setup(tmp_path, plugin_enabled=True, curl_status=401)

    assert proc.returncode == 0
    assert "rejected the configured bearer (401)" in proc.stdout
    assert "server MCP route usable" not in proc.stdout
    assert "done with warnings" in proc.stdout


def test_required_leases_warn_that_edits_will_block(tmp_path: Path) -> None:
    proc, _ = _run_setup(
        tmp_path,
        plugin_enabled=True,
        extra_env={
            "UNITARES_FILE_LEASES_ENABLED": "0",
            "UNITARES_FILE_LEASES_REQUIRED": "yes",
        },
    )

    assert proc.returncode == 0
    assert "pre-edit will block" in proc.stdout
    assert "Set it to 0" in proc.stdout
    assert "done with warnings" in proc.stdout
