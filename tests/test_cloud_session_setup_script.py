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
    health_status: int = 200,
    tool_status: int = 400,
    tool_body: str = "Missing 'name' field",
    health_exit: int = 0,
    tool_exit: int = 0,
    extra_env: dict[str, str] | None = None,
    script_args: list[str] | None = None,
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
printf 'curl %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
url=''
for arg in "$@"; do
  case "$arg" in http*) url="$arg" ;; esac
done
case "$url" in
  */health)
    printf '%s\\n%s' 'healthy' "$FAKE_HEALTH_STATUS"
    exit "$FAKE_HEALTH_EXIT"
    ;;
  */v1/tools/call)
    printf '%s\\n%s' "$FAKE_TOOL_BODY" "$FAKE_TOOL_STATUS"
    exit "$FAKE_TOOL_EXIT"
    ;;
  *)
    printf '%s\\n%s' 'not found' '404'
    ;;
esac
""",
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_COMMAND_LOG": str(command_log),
        "FAKE_PLUGIN_JSON": plugin_json,
        "FAKE_HEALTH_STATUS": str(health_status),
        "FAKE_TOOL_STATUS": str(tool_status),
        "FAKE_TOOL_BODY": tool_body,
        "FAKE_HEALTH_EXIT": str(health_exit),
        "FAKE_TOOL_EXIT": str(tool_exit),
        "UNITARES_SERVER_URL": "https://gov.example.test",
        "UNITARES_HTTP_API_TOKEN": "test-token",
        "UNITARES_CLOUD_PROXY_AUTH": "0",
        "UNITARES_FILE_LEASES_ENABLED": "0",
        "UNITARES_FILE_LEASES_REQUIRED": "0",
    }
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(SCRIPT), *(script_args or [])],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc, command_log.read_text()


def test_disabled_plugin_is_enabled_instead_of_treated_as_active(
    tmp_path: Path,
) -> None:
    proc, commands = _run_setup(tmp_path, plugin_enabled=False)

    assert proc.returncode == 0
    assert f"plugin enable {PLUGIN_ID}" in commands
    assert f"plugin install {PLUGIN_ID}" not in commands


def test_enabled_plugin_is_left_alone(tmp_path: Path) -> None:
    proc, commands = _run_setup(tmp_path, plugin_enabled=True)

    assert proc.returncode == 0
    assert f"plugin enable {PLUGIN_ID}" not in commands
    assert f"plugin install {PLUGIN_ID}" not in commands


def test_tool_probe_rejects_bad_bearer_even_when_health_is_green(
    tmp_path: Path,
) -> None:
    proc, commands = _run_setup(tmp_path, plugin_enabled=True, tool_status=401)

    assert proc.returncode == 0
    assert "rejected the configured bearer (401)" in proc.stdout
    assert "server tool route usable" not in proc.stdout
    assert "done with warnings" in proc.stdout
    assert "https://gov.example.test/health" in commands
    assert "https://gov.example.test/v1/tools/call" in commands


def test_tool_probe_rejects_generic_proxy_400(tmp_path: Path) -> None:
    proc, _ = _run_setup(
        tmp_path,
        plugin_enabled=True,
        tool_status=400,
        tool_body="proxy rejected request",
    )

    assert proc.returncode == 0
    assert "returned an unrecognized 400" in proc.stdout
    assert "server tool route usable" not in proc.stdout
    assert "done with warnings" in proc.stdout


def test_probe_reports_received_status_but_fails_on_curl_error(tmp_path: Path) -> None:
    proc, _ = _run_setup(
        tmp_path,
        plugin_enabled=True,
        health_exit=28,
        tool_exit=28,
    )

    assert proc.returncode == 0
    assert "transfer failed after HTTP 200 (curl 28)" in proc.stdout
    assert "transfer failed after HTTP 400 (curl 28)" in proc.stdout
    assert "server health route usable" not in proc.stdout
    assert "server tool route usable" not in proc.stdout
    assert "done with warnings" in proc.stdout


def test_missing_bearer_warning_names_authentication_not_attribution(
    tmp_path: Path,
) -> None:
    proc, _ = _run_setup(
        tmp_path,
        plugin_enabled=True,
        tool_status=401,
        extra_env={"UNITARES_HTTP_API_TOKEN": ""},
    )

    assert proc.returncode == 0
    assert "REST hooks need another accepted" in proc.stdout
    assert "authentication path or they receive 401" in proc.stdout
    assert "Attribution is session-bound" in proc.stdout
    assert "writes to the server will not be attributable" not in proc.stdout


def test_proxy_bearer_defers_auth_probe_until_after_setup(tmp_path: Path) -> None:
    proc, commands = _run_setup(
        tmp_path,
        plugin_enabled=True,
        extra_env={
            "UNITARES_HTTP_API_TOKEN": "",
            "UNITARES_CLOUD_PROXY_AUTH": "1",
        },
    )

    assert proc.returncode == 0
    assert "hook network/authentication probes deferred" in proc.stdout
    assert "environment proxy" in proc.stdout
    assert "not verify it automatically" in proc.stdout
    assert "with --verify-runtime" in proc.stdout
    assert "https://gov.example.test/health" not in commands
    assert "https://gov.example.test/v1/tools/call" not in commands
    assert "hook authentication remains UNVERIFIED" in proc.stdout
    assert "done with warnings" in proc.stdout


def test_runtime_preflight_verifies_proxy_injected_bearer(tmp_path: Path) -> None:
    proc, commands = _run_setup(
        tmp_path,
        plugin_enabled=True,
        extra_env={
            "UNITARES_HTTP_API_TOKEN": "",
            "UNITARES_CLOUD_PROXY_AUTH": "1",
        },
        script_args=["--verify-runtime"],
    )

    assert proc.returncode == 0
    assert "https://gov.example.test/health" in commands
    assert "https://gov.example.test/v1/tools/call" in commands
    assert "server tool route usable (authenticated validation response)" in proc.stdout
    assert "done with warnings" not in proc.stdout


def test_runtime_preflight_rejects_bad_proxy_bearer(tmp_path: Path) -> None:
    proc, _ = _run_setup(
        tmp_path,
        plugin_enabled=True,
        tool_status=401,
        extra_env={
            "UNITARES_HTTP_API_TOKEN": "",
            "UNITARES_CLOUD_PROXY_AUTH": "1",
        },
        script_args=["--verify-runtime"],
    )

    assert proc.returncode == 0
    assert "requires a bearer (401)" in proc.stdout
    assert "server tool route usable" not in proc.stdout
    assert "done with warnings" in proc.stdout


def test_server_url_with_mcp_suffix_is_rejected_as_hook_incompatible(
    tmp_path: Path,
) -> None:
    proc, commands = _run_setup(
        tmp_path,
        plugin_enabled=True,
        extra_env={"UNITARES_SERVER_URL": "https://gov.example.test/mcp"},
    )

    assert proc.returncode == 0
    assert "must be the server base URL, without /mcp" in proc.stdout
    assert "https://gov.example.test/mcp/health" in commands
    assert "https://gov.example.test/mcp/v1/tools/call" in commands
    assert "done with warnings" in proc.stdout


def test_plain_http_warning_lowers_final_preflight_verdict(tmp_path: Path) -> None:
    proc, _ = _run_setup(
        tmp_path,
        plugin_enabled=True,
        extra_env={"UNITARES_SERVER_URL": "http://gov.example.test"},
    )

    assert proc.returncode == 0
    assert "WARN not https://" in proc.stdout
    assert "done with warnings" in proc.stdout


def test_proxy_mode_rejects_nonstandard_https_port_before_deferred_probe(
    tmp_path: Path,
) -> None:
    proc, commands = _run_setup(
        tmp_path,
        plugin_enabled=True,
        extra_env={
            "UNITARES_SERVER_URL": "https://gov.example.test:8767",
            "UNITARES_HTTP_API_TOKEN": "",
            "UNITARES_CLOUD_PROXY_AUTH": "1",
        },
    )

    assert proc.returncode == 0
    assert "uses port 8767" in proc.stdout
    assert "requires HTTPS on port 443" in proc.stdout
    assert "https://gov.example.test:8767/health" not in commands
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
