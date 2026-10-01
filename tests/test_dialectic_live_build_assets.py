"""Contract tests for dialectic_live's boot/deploy asset build.

`elixir/dialectic_live/scripts/build-assets.sh` decides whether a failed
asset build may fall back to the previous digest (lenient, every boot) or
must fail (--strict, deploys). These tests run the real script in a scratch
app directory with `mix` and the CLI-repair helper stubbed. The `mix` stub
follows the `assets.digest` alias contract in mix.exs: refuse unless the
script's build lock is held, clear the marker, digest, write the marker only
on success.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "elixir" / "dialectic_live" / "scripts" / "build-assets.sh"

MIX_STUB = """#!/usr/bin/env bash
set -eu
app="$PWD"
case "$1" in
  assets.compile) sleep "${COMPILE_SLEEP:-0}"; exit "${COMPILE_RC:-0}" ;;
  assets.digest)
    [ "${DIALECTIC_LIVE_ASSETS_LOCKED:-}" = 1 ] || { echo "digest without the build lock" >&2; exit 98; }
    rm -f "$app/_build/assets-digest.ok"
    echo "{\\"digest\\": \\"new\\"}" > "$app/priv/static/cache_manifest.json"
    [ "${DIGEST_RC:-0}" -eq 0 ] || exit "$DIGEST_RC"
    mkdir -p "$app/_build" && touch "$app/_build/assets-digest.ok"
    ;;
  *) echo "unexpected mix $*" >&2; exit 99 ;;
esac
"""


def _app(tmp_path: Path, *, previous_build: bool) -> Path:
    app = tmp_path / "dialectic_live"
    (app / "scripts").mkdir(parents=True)
    (app / "priv" / "static").mkdir(parents=True)
    (app / "_build").mkdir()
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
    if previous_build:
        (app / "priv" / "static" / "cache_manifest.json").write_text(
            '{"digest": "old"}'
        )
        (app / "_build" / "assets-digest.ok").touch()
    return app


def _run(app: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    full_env = {
        **os.environ,
        "PATH": f"{app.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "MIX_ENV": "prod",
        "DIALECTIC_LIVE_ASSETS_LOCK_WAIT": "2",
        **env,
    }
    return subprocess.run(
        [str(app / "scripts" / "build-assets.sh"), *args],
        cwd=app,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _manifest(app: Path) -> str:
    return (app / "priv" / "static" / "cache_manifest.json").read_text()


def _marker(app: Path) -> bool:
    return (app / "_build" / "assets-digest.ok").exists()


def test_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & 0o111


def test_clean_build_digests_and_marks(tmp_path: Path) -> None:
    app = _app(tmp_path, previous_build=False)
    proc = _run(app)
    assert proc.returncode == 0, proc.stderr
    assert "new" in _manifest(app)
    assert _marker(app)


def test_lenient_compile_failure_serves_completed_previous_build(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, previous_build=True)
    proc = _run(app, COMPILE_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert "serving the previous build" in proc.stderr
    assert "old" in _manifest(app)


def test_cli_repair_failure_counts_as_compile_failure(tmp_path: Path) -> None:
    app = _app(tmp_path, previous_build=True)
    proc = _run(app, PREPARE_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert "serving the previous build" in proc.stderr


def test_lenient_compile_failure_without_previous_build_fails(tmp_path: Path) -> None:
    app = _app(tmp_path, previous_build=False)
    proc = _run(app, COMPILE_RC="1")
    assert proc.returncode == 1
    assert "no completed previous build" in proc.stderr


def test_manifest_without_marker_is_not_a_previous_build(tmp_path: Path) -> None:
    app = _app(tmp_path, previous_build=True)
    (app / "_build" / "assets-digest.ok").unlink()
    proc = _run(app, COMPILE_RC="1")
    assert proc.returncode == 1
    assert "no completed previous build" in proc.stderr


def test_strict_compile_failure_fails_even_with_previous_build(tmp_path: Path) -> None:
    app = _app(tmp_path, previous_build=True)
    proc = _run(app, "--strict", COMPILE_RC="1")
    assert proc.returncode == 1
    assert "(--strict)" in proc.stderr


def test_digest_failure_is_fatal_and_clears_the_marker(tmp_path: Path) -> None:
    app = _app(tmp_path, previous_build=True)
    proc = _run(app, DIGEST_RC="1")
    assert proc.returncode != 0
    assert not _marker(app)
    # The interrupted digest is never mistaken for a previous build.
    proc = _run(app, COMPILE_RC="1")
    assert proc.returncode == 1
    assert "no completed previous build" in proc.stderr


def test_held_lock_blocks_a_second_build(tmp_path: Path) -> None:
    app = _app(tmp_path, previous_build=True)
    with open(app / "_build" / "assets-build.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = _run(app)
    assert proc.returncode == 1
    assert "still holds" in proc.stderr
    assert "old" in _manifest(app)


def test_lock_is_released_when_the_build_exits(tmp_path: Path) -> None:
    app = _app(tmp_path, previous_build=False)
    assert _run(app).returncode == 0
    with open(app / "_build" / "assets-build.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held


def test_lock_is_held_for_the_whole_build(tmp_path: Path) -> None:
    # perl takes the flock and exits; the lock must stay with the script's fd.
    app = _app(tmp_path, previous_build=False)
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "MIX_ENV": "prod",
        "COMPILE_SLEEP": "3",
    }
    build = subprocess.Popen(
        [str(app / "scripts" / "build-assets.sh")],
        cwd=app,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        with open(app / "_build" / "assets-build.lock", "a") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held = True
            else:
                held = False
        assert held, "build lock was not held while the build ran"
    finally:
        assert build.wait(timeout=30) == 0
