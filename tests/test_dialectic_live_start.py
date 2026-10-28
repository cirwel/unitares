"""dialectic_live's launcher, plist template and deploy script (KG 2026-10-27T22:16:58.919702).

- start.sh takes only the app's own names from the selected file, never every
  credential in it, and stops cleanly (exit 0) when not configured.
- The plist template says loading starts the app, and restarts it only after
  a failing exit.
- The deploy script rolls the shared tree back when the build fails and
  reloads a changed plist instead of kickstarting a cached definition.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START = REPO_ROOT / "elixir" / "dialectic_live" / "scripts" / "start.sh"
TEMPLATE = REPO_ROOT / "scripts" / "ops" / "com.unitares.dialectic-live.plist.template"
DEPLOY = REPO_ROOT / "scripts" / "ops" / "deploy-dialectic-live.sh"

MIX_STUB = """#!/usr/bin/env bash
case "$1" in
  deps.get) exit 0 ;;
  phx.server)
    [ -z "${OTHER_SECRET:-}" ] && echo "other=absent"
    echo "skb=${SECRET_KEY_BASE:-}"
    echo "token=${UNITARES_HTTP_API_TOKEN:-}"
    echo "ws=${GOVERNANCE_WS_URL:-}"
    echo "port=${PORT:-}"
    ;;
esac
"""


def _app(tmp_path: Path) -> tuple[Path, dict]:
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    script = app / "scripts" / "start.sh"
    script.write_text(START.read_text())
    script.chmod(0o755)
    assets = app / "scripts" / "build-assets.sh"
    assets.write_text("#!/usr/bin/env bash\nexit 0\n")
    assets.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mix = bin_dir / "mix"
    mix.write_text(MIX_STUB)
    mix.chmod(0o755)
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "MIX_ENV": "prod",
    }
    return app, env


def _start(app: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(app / "scripts" / "start.sh")], env=env,
                          capture_output=True, text=True, check=False)


def test_only_the_apps_own_names_come_from_the_file(tmp_path):
    app, env = _app(tmp_path)
    shared = tmp_path / "shared.env"
    shared.write_text(
        "DIALECTIC_LIVE_SECRET_KEY_BASE='skb value with spaces'\n"
        "UNITARES_HTTP_API_TOKEN=tok\n"
        "OTHER_SECRET='must stay out'\n"
        "export GOVERNANCE_WS_URL=ws://127.0.0.1:9999/ws/eisv\n"
    )
    shared.chmod(0o600)
    proc = _start(app, {**env, "UNITARES_SECRETS_ENV": str(shared)})
    assert proc.returncode == 0, proc.stderr
    assert "other=absent" in proc.stdout
    assert "skb=skb value with spaces" in proc.stdout
    assert "token=tok" in proc.stdout
    assert "ws=ws://127.0.0.1:9999/ws/eisv" in proc.stdout
    assert "port=8790" in proc.stdout


def test_values_never_reach_the_log(tmp_path):
    app, env = _app(tmp_path)
    shared = tmp_path / "shared.env"
    shared.write_text("DIALECTIC_LIVE_SECRET_KEY_BASE=secret-value\n")
    shared.chmod(0o600)
    env_with = {**env, "UNITARES_SECRETS_ENV": str(shared)}
    proc = _start(app, env_with)
    assert "secret-value" not in proc.stderr


def test_without_a_secret_key_base_it_stops_cleanly(tmp_path):
    app, env = _app(tmp_path)
    proc = _start(app, env)
    assert proc.returncode == 0
    assert "SECRET_KEY_BASE (or DIALECTIC_LIVE_SECRET_KEY_BASE) is unset" in proc.stderr
    assert "will not restart it" in proc.stderr


def test_a_relative_settings_path_stops_cleanly(tmp_path):
    app, env = _app(tmp_path)
    proc = _start(app, {**env, "UNITARES_SECRETS_ENV": "relative.env"})
    assert proc.returncode == 0
    assert "must name an absolute" in proc.stderr


def test_the_plist_restarts_only_after_a_failing_exit():
    rendered = TEMPLATE.read_text().replace("__UNITARES_ROOT__", "/r").replace("__HOME__", "/h")
    plist = plistlib.loads(rendered.encode())
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert "RunAtLoad" not in plist
    text = TEMPLATE.read_text()
    assert "Loading this plist starts the app" in text
    assert "NOT loaded by" not in text


def test_the_deploy_rolls_back_a_failed_build_and_reloads_a_changed_plist():
    text = DEPLOY.read_text()
    assert text.index("roll_back() {") < text.index('echo "[deploy] compiling dialectic_live')
    build = text.index('if ! (\n  cd "$DEPLOY/elixir/dialectic_live" &&')
    block = text[build:text.index("); then", build)]
    assert block.count("&&") == 4  # set -e is off inside a condition
    assert 'deploy_lib_restart_service "$TAG" "gui/$UID_NUM" "$LABEL" "$PLIST"' in text
    assert "launchctl kickstart" not in text
