"""Launchers read only selected credential files; tests stop before application boot."""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STARTS = ["lease_plane", "agent_orchestrator", "wave3a_handlers", "sentinel", "dialectic_live"]


def _source_phase(app, home, **environment):
    path = ROOT / "elixir" / app / "scripts/start.sh"
    content = path.read_text()
    # Execute the actual source-selection phase, stopping before dependency
    # fetching, cookies, databases, subprocesses, or service boot can occur.
    boundary = 'export MIX_ENV=' if app == "dialectic_live" else "# Homebrew Elixir"
    phase = content[:content.index(boundary)]
    return subprocess.run(
        ["bash", "-c", phase + '\nprintf "%s|%s" "${TEST_SELECTED-unset}" "${UNITARES_HTTP_API_TOKEN-unset}"', str(path)],
        env={"PATH": os.defpath, "HOME": str(home), **environment},
        capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("app", STARTS)
def test_startup_never_discovers_home_secret_files(app, tmp_path):
    for directory in ("unitares", "cirwel"):
        path = tmp_path / ".config" / directory / "secrets.env"
        path.parent.mkdir(parents=True)
        path.write_text("export TEST_SELECTED=unexpected\n")
    result = _source_phase(app, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "unset|unset"


# dialectic_live takes only its own names from the file (KG
# 2026-10-27T22:16:58.919702), so it is pinned separately below.
WHOLE_FILE_STARTS = [app for app in STARTS if app != "dialectic_live"]


@pytest.mark.parametrize("app", WHOLE_FILE_STARTS)
def test_startup_sources_only_the_explicit_file(app, tmp_path):
    selected = tmp_path / "chosen.env"
    selected.write_text("export TEST_SELECTED=selected\n")
    result = _source_phase(app, tmp_path, UNITARES_SECRETS_ENV=str(selected))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "selected|unset"


def test_dialectic_live_takes_only_its_own_names_from_the_explicit_file(tmp_path):
    selected = tmp_path / "chosen.env"
    selected.write_text("export TEST_SELECTED=foreign\nexport UNITARES_HTTP_API_TOKEN=own\n")
    result = _source_phase("dialectic_live", tmp_path, UNITARES_SECRETS_ENV=str(selected))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "unset|own"


@pytest.mark.parametrize("app", WHOLE_FILE_STARTS)
def test_explicit_missing_source_refuses_before_application_boot(app, tmp_path):
    result = _source_phase(app, tmp_path, UNITARES_SECRETS_ENV=str(tmp_path / "absent.env"))
    assert result.returncode == 1
    assert "absolute, readable regular file" in result.stderr


def test_dialectic_live_stops_cleanly_on_a_missing_source(tmp_path):
    # Exit 0, not 1: its plist restarts it only after a failing exit, and a
    # restart cannot fix a missing file.
    result = _source_phase("dialectic_live", tmp_path, UNITARES_SECRETS_ENV=str(tmp_path / "absent.env"))
    assert result.returncode == 0
    assert "absolute, readable regular file" in result.stderr
    assert "will not restart it" in result.stderr


def test_sentinel_keeps_provisioned_bearer_over_selected_file(tmp_path):
    selected = tmp_path / "chosen.env"
    selected.write_text("export UNITARES_HTTP_API_TOKEN=file-value\n")
    result = _source_phase("sentinel", tmp_path, UNITARES_SECRETS_ENV=str(selected),
                           UNITARES_HTTP_API_TOKEN="service-value")
    assert result.returncode == 0
    assert result.stdout == "unset|service-value"
