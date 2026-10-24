"""Exercise real restart helpers with fake launchd and no service/secret access."""

import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts/ops/deploy-lib.sh"
NUDGE = ROOT / "scripts/ops/nudge-lease-plane.sh"
HEALTHCHECK = ROOT / "elixir/lease_plane/scripts/ops/lease_plane_acquire_healthcheck.sh"
SELECTED = "/fixture/selected file.env"  # Deliberately nonexistent: never read.


def metadata(selected=None, *, block="environment", quote=False):
    value = json.dumps(selected) if quote else selected
    entry = f"\tUNITARES_SECRETS_ENV => {value}\n" if selected is not None else ""
    return f"{block} = {{\n{entry}\tSECRET => never-print-this\n}}\n"


@pytest.fixture
def host(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    launcher = binary / "launchctl"
    launcher.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
base = pathlib.Path(os.environ["FAKE_LAUNCHD"])
with (base / "calls").open("a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
command = sys.argv[1]
if command == "print":
    if (base / "unloaded").exists():
        sys.exit(1)
    print((base / "loaded").read_text(), end="")
elif command == "bootout":
    (base / "unloaded").touch()
elif command == "bootstrap":
    (base / "unloaded").unlink(missing_ok=True)
    (base / "loaded").write_text((base / "fresh").read_text())
''')
    launcher.chmod(0o755)
    for name, body in {"sleep": ":", "curl": 'printf "%s" "${FAKE_HTTP:-200}"'}.items():
        script = binary / name
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
    (tmp_path / "calls").touch()
    (tmp_path / "loaded").write_text(metadata())
    (tmp_path / "fresh").write_text(metadata(SELECTED))
    plist = tmp_path / "job.plist"
    plist.write_bytes(plistlib.dumps({"Label": "test.svc", "EnvironmentVariables": {"UNITARES_SECRETS_ENV": SELECTED}}))
    env = {"PATH": str(binary) + os.pathsep + os.defpath, "HOME": str(tmp_path),
           "FAKE_LAUNCHD": str(tmp_path), "UNITARES_DEPLOY_STATE_DIR": str(tmp_path / "state"),
           "UNITARES_MCP_DEPLOY": "/fixture/shared", "UNITARES_LEASE_PLANE_PLIST": str(plist)}
    return tmp_path, plist, env


def run(host, command):
    _, _, env = host
    return subprocess.run(["bash", "-c", f'. {shlex.quote(str(LIB))}\n{command}'],
                          env=env, text=True, capture_output=True, check=False)


def guard(host):
    return run(host, f'deploy_lib_require_loaded_secrets_env test {shlex.quote(str(host[1]))} gui/501/test.svc')


@pytest.mark.parametrize("cached", [metadata(), metadata("/fixture/old.env"),
                                     metadata(SELECTED, block="inherited environment"), "unrecognized"])
def test_missing_stale_or_unverifiable_cached_selector_refuses_without_values(host, cached):
    (host[0] / "loaded").write_text(cached)
    result = guard(host)
    assert result.returncode == 1
    assert "cached UNITARES_SECRETS_ENV" in result.stderr
    assert "never-print-this" not in result.stdout + result.stderr
    assert SELECTED not in result.stdout + result.stderr


@pytest.mark.parametrize("quote", [False, True])
def test_matching_cached_selector_does_not_read_selected_file(host, quote):
    (host[0] / "loaded").write_text(metadata(SELECTED, quote=quote))
    assert guard(host).returncode == 0


@pytest.mark.parametrize("env", [{}, {"UNITARES_SECRETS_ENV": ""}, {"TOKEN": "injected-directly"}])
def test_unconfigured_install_does_not_require_launchd_selection(host, env):
    host[1].write_bytes(plistlib.dumps({"EnvironmentVariables": env}))
    assert guard(host).returncode == 0
    assert (host[0] / "calls").read_text() == ""


@pytest.mark.parametrize("data", [b"not a plist", plistlib.dumps({"EnvironmentVariables": {"UNITARES_SECRETS_ENV": "relative.env"}})])
def test_unreadable_or_invalid_selection_refuses(host, data):
    host[1].write_bytes(data)
    assert guard(host).returncode == 1


def restart(host, *, force=False):
    return run(host, f'{"DEPLOY_LIB_FORCE_RELOAD=1 " if force else ""}deploy_lib_restart_service test gui/501 test.svc {shlex.quote(str(host[1]))}')


def test_first_adoption_refuses_stale_cache_before_sidecar_or_kickstart(host):
    result = restart(host)
    assert result.returncode == 1
    assert not (host[0] / "state/test.svc.plist.sha256").exists()
    assert "kickstart" not in (host[0] / "calls").read_text()
    assert "bootout" not in (host[0] / "calls").read_text()


def test_matching_cache_allows_adoption_and_unchanged_restart(host):
    (host[0] / "loaded").write_text(metadata(SELECTED))
    assert restart(host).returncode == 0
    assert restart(host).returncode == 0
    assert (host[0] / "calls").read_text().count("kickstart") == 2


def test_hashless_restart_still_refuses_stale_cache(host):
    result = run(host, f'_deploy_lib_sha256() {{ return 1; }}\ndeploy_lib_restart_service test gui/501 test.svc {shlex.quote(str(host[1]))}')
    assert result.returncode == 1
    assert "kickstart" not in (host[0] / "calls").read_text()


def test_controlled_reload_accepts_fresh_selection_and_records_it(host):
    assert restart(host, force=True).returncode == 0
    calls = (host[0] / "calls").read_text()
    assert "bootout" in calls and "bootstrap" in calls and "kickstart" not in calls
    assert (host[0] / "state/test.svc.plist.sha256").exists()


def test_reload_with_missing_selection_fails_without_sidecar(host):
    (host[0] / "fresh").write_text(metadata())
    assert restart(host, force=True).returncode == 1
    assert not (host[0] / "state/test.svc.plist.sha256").exists()


def test_indirect_nudge_refuses_stale_selection_without_restart(host):
    result = run(host, f'bash {shlex.quote(str(NUDGE))} --reason fixture')
    assert result.returncode == 1
    assert "kickstart" not in (host[0] / "calls").read_text()


@pytest.mark.parametrize("code, expected", [("200", 0), ("401", 0), ("403", 0), ("503", 1), ("000", 1)])
def test_nudge_readiness_rejects_serving_but_unavailable_plane(host, code, expected):
    (host[0] / "loaded").write_text(metadata(SELECTED))
    host[2]["FAKE_HTTP"] = code
    result = run(host, f'bash {shlex.quote(str(NUDGE))} --reason fixture')
    assert result.returncode == expected
    assert "kickstart" in (host[0] / "calls").read_text()
    if expected:
        assert "plane answering" not in result.stdout


def test_nudge_preserves_unloaded_job(host):
    (host[0] / "unloaded").touch()
    result = run(host, f'bash {shlex.quote(str(NUDGE))} --reason fixture')
    assert result.returncode == 0
    assert "kickstart" not in (host[0] / "calls").read_text()


def test_acquire_backstop_refuses_stale_cache_without_remediation(host):
    # Actual function definitions, stopping before network probes. Paths are
    # isolated; alert/publication stubs ensure no live finding or service call.
    definitions = HEALTHCHECK.read_text().split("\nfail() {", 1)[0]
    command = definitions + f'''
HEALTHCHECK_REPO_ROOT={shlex.quote(str(ROOT))}
AUTORESTART=1
REMEDIATE_FAILURES=1
RESTART_MARKER={shlex.quote(str(host[0] / "restart-marker"))}
alert() {{ echo "$1"; }}
post_finding() {{ :; }}
maybe_remediate 1
'''
    result = run(host, command)
    assert "auto-restart REFUSED" in result.stdout
    assert "kickstart" not in (host[0] / "calls").read_text()
