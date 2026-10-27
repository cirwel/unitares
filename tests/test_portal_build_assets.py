"""Contract tests for the operator hub's boot/deploy stylesheet build.

`elixir/portal/scripts/build-assets.sh` follows dialectic_live's design
(tests/test_dialectic_live_build_assets.py): it skips the build when the one
on disk is this checkout's, builds otherwise, and on a failed build either
falls back to what is on disk (lenient, every boot) or fails (--strict,
deploys). The hub builds one file, priv/static/assets/css/hub.css, and no
digest. These tests run the real script inside a scratch git checkout with
`mix` and the CLI-repair helper stubbed; the `mix` stub writes new content on
every build.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP = REPO_ROOT / "elixir" / "portal"
SCRIPT = APP / "scripts" / "build-assets.sh"
START = APP / "scripts" / "start.sh"
DEPLOY = REPO_ROOT / "scripts" / "ops" / "deploy-portal.sh"
TEMPLATE = REPO_ROOT / "scripts" / "ops" / "com.unitares.portal.plist.template"

MIX_STUB = """#!/usr/bin/env bash
set -eu
echo "$*" >> "$MIX_CALLS"
case "$1" in
  assets.deploy)
    [ "${BUILD_RC:-0}" -eq 0 ] || exit "$BUILD_RC"
    mkdir -p priv/static/assets/css
    echo "css $(date +%s)-$$-$RANDOM" > priv/static/assets/css/hub.css
    ;;
  *) echo "unexpected mix $*" >&2; exit 99 ;;
esac
"""

GITIGNORE = "/_build/\n/priv/static/assets/css/hub.css\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


def _app(tmp_path: Path, *, git: bool = True) -> Path:
    repo = tmp_path / "repo"
    app = repo / "elixir" / "portal"
    (app / "scripts").mkdir(parents=True)
    (app / "lib").mkdir()
    (app / "lib" / "page.ex").write_text("v1\n")
    (app / ".gitignore").write_text(GITIGNORE)
    script = app / "scripts" / "build-assets.sh"
    script.write_text(SCRIPT.read_text())
    script.chmod(0o755)
    helper = app / "scripts" / "prepare-asset-binaries.sh"
    helper.write_text('#!/usr/bin/env bash\nexit "${PREPARE_RC:-0}"\n')
    helper.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mix = bin_dir / "mix"
    mix.write_text(MIX_STUB)
    mix.chmod(0o755)
    if git:
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
    return app


def _run(app: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    tmp = app.parent.parent.parent
    full_env = {
        **os.environ,
        "PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "MIX_ENV": "prod",
        "MIX_CALLS": str(tmp / "mix-calls"),
        "PORTAL_ASSETS_LOCK_WAIT": "2",
        **env,
    }
    full_env["GIT_CEILING_DIRECTORIES"] = str(tmp)
    return subprocess.run(
        [str(app / "scripts" / "build-assets.sh"), *args],
        cwd=app,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _builds(app: Path) -> int:
    calls = app.parent.parent.parent / "mix-calls"
    return calls.read_text().splitlines().count("assets.deploy") if calls.exists() else 0


def _output(app: Path) -> Path:
    return app / "priv" / "static" / "assets" / "css" / "hub.css"


def _stamp(app: Path) -> Path:
    return app / "_build" / "assets.stamp"


def test_scripts_are_executable() -> None:
    for script in (SCRIPT, START, APP / "scripts" / "prepare-asset-binaries.sh", DEPLOY):
        assert script.stat().st_mode & 0o111, script


def test_first_boot_builds_and_stamps(tmp_path: Path) -> None:
    app = _app(tmp_path)
    proc = _run(app)
    assert proc.returncode == 0, proc.stderr
    assert _builds(app) == 1 and _output(app).exists() and _stamp(app).exists()


def test_steady_state_boot_does_no_asset_work(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app, "--strict").returncode == 0
    proc = _run(app)
    assert proc.returncode == 0, proc.stderr
    assert "build is current" in proc.stdout
    assert _builds(app) == 1


def test_a_failed_boot_build_serves_the_one_on_disk(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    (app / "lib" / "page.ex").write_text("v2\n")
    _git(app.parent.parent, "commit", "-qam", "v2")
    proc = _run(app, BUILD_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert "serving the build already on disk" in proc.stderr


def test_a_failed_boot_build_with_nothing_on_disk_exits_1(tmp_path: Path) -> None:
    app = _app(tmp_path)
    proc = _run(app, BUILD_RC="1")
    assert proc.returncode == 1
    assert "no previous build to serve" in proc.stderr


def test_strict_always_builds_and_fails_on_failure(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app, "--strict").returncode == 0
    assert _run(app, "--strict").returncode == 0
    assert _builds(app) == 2
    proc = _run(app, "--strict", PREPARE_RC="1")
    assert proc.returncode == 1 and "(--strict)" in proc.stderr


def test_new_commit_and_uncommitted_changes_rebuild(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    (app / "lib" / "page.ex").write_text("v2\n")
    _git(app.parent.parent, "commit", "-qam", "v2")
    assert _run(app).returncode == 0
    (app / "lib" / "page.ex").write_text("edited\n")
    assert _run(app).returncode == 0
    assert _builds(app) == 3


def test_another_writer_or_a_missing_file_invalidates_the_stamp(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    _output(app).write_text("someone else\n")
    assert _run(app).returncode == 0
    _output(app).unlink()
    assert _run(app).returncode == 0
    assert _builds(app) == 3


def test_outside_a_git_checkout_every_boot_builds(tmp_path: Path) -> None:
    app = _app(tmp_path, git=False)
    assert _run(app).returncode == 0
    assert _run(app).returncode == 0
    assert _builds(app) == 2
    assert not _stamp(app).exists()


# An unconfigured hub exits 0, which the plist does not restart: a restart
# cannot fix configuration, and a non-zero exit would loop under KeepAlive.
def test_start_without_a_secret_key_base_stops_cleanly(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PORTAL_", "UNITARES_SECRETS"))}
    proc = subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "PORTAL_SECRET_KEY_BASE is unset" in proc.stderr
    assert "will not restart it" in proc.stderr


def test_start_with_a_relative_settings_path_stops_cleanly() -> None:
    env = {**os.environ, "UNITARES_SECRETS_ENV": "relative/file.env"}
    proc = subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "UNITARES_SECRETS_ENV must name an absolute" in proc.stderr


def test_the_plist_restarts_only_after_a_failing_exit() -> None:
    import plistlib

    rendered = TEMPLATE.read_text().replace("__UNITARES_ROOT__", "/r").replace("__HOME__", "/h")
    plist = plistlib.loads(rendered.encode())
    # Any KeepAlive implies RunAtLoad (launchd.plist(5)); the conditional form
    # keeps a clean exit (unconfigured) from becoming a restart loop.
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert "RunAtLoad" not in plist
    assert plist["EnvironmentVariables"]["PORTAL_PORT"] == "8791"
    assert plist["ProgramArguments"] == ["/bin/bash", "/r/elixir/portal/scripts/start.sh"]
    assert "Loading this plist starts the hub" in TEMPLATE.read_text()


def test_the_deploy_checks_the_hubs_exact_origin_and_fails_on_either_answer() -> None:
    text = DEPLOY.read_text()
    assert 'socket_status "http://127.0.0.1:$PORT"' in text
    assert 'socket_status "http://evil.example"' in text
    assert 'LABEL="com.unitares.portal"' in text
    # A wrong socket answer rolls back; it is not a warning.
    check = text.index('if [ "$own" != "101" ] || [ "$foreign" != "403" ]; then')
    assert text.index("roll_back", check) < text.index("fi", check)
    assert "WARNING — the LiveView socket" not in text


def test_the_deploy_reloads_a_changed_plist() -> None:
    text = DEPLOY.read_text()
    assert 'deploy_lib_restart_service "$TAG" "gui/$UID_NUM" "$LABEL" "$PLIST"' in text
    assert "launchctl kickstart" not in text
