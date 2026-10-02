"""Contract tests for dialectic_live's boot/deploy asset build.

`elixir/dialectic_live/scripts/build-assets.sh` skips the build when the
build on disk is the one for this checkout, builds otherwise, and on a failed
build either falls back to what is on disk (lenient, every boot) or fails
(--strict, deploys). These tests run the real script inside a scratch git
checkout with `mix` and the CLI-repair helper stubbed. Like phx.digest, the
`mix` stub writes a digested file and a manifest naming it, with new content
on every build.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "elixir" / "dialectic_live" / "scripts" / "build-assets.sh"

MIX_STUB = """#!/usr/bin/env bash
set -eu
echo "$*" >> "$MIX_CALLS"
case "$1" in
  assets.deploy)
    sleep "${BUILD_SLEEP:-0}"
    [ "${BUILD_RC:-0}" -eq 0 ] || exit "$BUILD_RC"
    id="$(date +%s)-$$-$RANDOM"
    mkdir -p priv/static/assets
    echo "css $id" > "priv/static/assets/app-$id.css"
    echo "{\\"latest\\": {\\"assets/app.css\\": \\"assets/app-$id.css\\"}}" > priv/static/cache_manifest.json
    ;;
  *) echo "unexpected mix $*" >&2; exit 99 ;;
esac
"""

GITIGNORE = "/_build/\n/priv/static/cache_manifest.json\n/priv/static/assets/\n"


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
    app = repo / "elixir" / "dialectic_live"
    (app / "scripts").mkdir(parents=True)
    (app / "priv" / "static").mkdir(parents=True)
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
        "DIALECTIC_LIVE_ASSETS_LOCK_WAIT": "2",
        **env,
    }
    # Outside a checkout, git must not find an enclosing repository.
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
    if not calls.exists():
        return 0
    return calls.read_text().splitlines().count("assets.deploy")


def _manifest(app: Path) -> Path:
    return app / "priv" / "static" / "cache_manifest.json"


def _stamp(app: Path) -> Path:
    return app / "_build" / "assets.stamp"


def test_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & 0o111


def test_first_boot_builds_and_stamps(tmp_path: Path) -> None:
    app = _app(tmp_path)
    proc = _run(app)
    assert proc.returncode == 0, proc.stderr
    assert _builds(app) == 1
    assert _manifest(app).exists()
    assert _stamp(app).exists()


def test_steady_state_boot_does_no_asset_work(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app, "--strict").returncode == 0  # the deploy build
    proc = _run(app)
    assert proc.returncode == 0, proc.stderr
    assert "build is current" in proc.stdout
    assert _builds(app) == 1


def test_steady_state_boot_never_fails_even_when_a_build_would(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    proc = _run(app, BUILD_RC="1", PREPARE_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert _builds(app) == 1


def test_new_commit_triggers_a_rebuild(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    (app / "lib" / "page.ex").write_text("v2\n")
    _git(app.parent.parent, "commit", "-qam", "v2")
    assert _run(app).returncode == 0
    assert _builds(app) == 2


def test_uncommitted_changes_always_rebuild(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    (app / "lib" / "page.ex").write_text("edited\n")
    assert _run(app).returncode == 0
    assert _run(app).returncode == 0
    assert _builds(app) == 3


def test_a_build_by_another_writer_invalidates_the_stamp(tmp_path: Path) -> None:
    # e.g. a deploy rollback that rebuilt with an older script, then a
    # fast-forward back to this tree by another service's deploy.
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    _manifest(app).write_text('{"digest": "someone else"}')
    assert _run(app).returncode == 0
    assert _builds(app) == 2


def test_missing_manifest_rebuilds_despite_stamp(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    _manifest(app).unlink()
    assert _run(app).returncode == 0
    assert _builds(app) == 2


def test_outside_a_git_checkout_every_boot_builds(tmp_path: Path) -> None:
    app = _app(tmp_path, git=False)
    assert _run(app).returncode == 0
    assert _run(app).returncode == 0
    assert _builds(app) == 2
    assert not _stamp(app).exists()


def test_strict_always_builds(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    assert _run(app, "--strict").returncode == 0
    assert _builds(app) == 2


def test_lenient_failure_serves_the_build_on_disk(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    (app / "lib" / "page.ex").write_text("v2\n")
    _git(app.parent.parent, "commit", "-qam", "v2")
    before = _manifest(app).read_text()
    proc = _run(app, BUILD_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert "serving the build already on disk" in proc.stderr
    assert _manifest(app).read_text() == before
    # The failed build left no stamp, so the next boot tries again.
    assert not _stamp(app).exists()
    assert _run(app).returncode == 0
    assert _builds(app) == 3


def test_cli_repair_failure_counts_as_a_build_failure(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _manifest(app).write_text('{"digest": "old"}')
    proc = _run(app, PREPARE_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert "serving the build already on disk" in proc.stderr
    assert _builds(app) == 0


def test_lenient_failure_with_nothing_on_disk_fails(tmp_path: Path) -> None:
    app = _app(tmp_path)
    proc = _run(app, BUILD_RC="1")
    assert proc.returncode == 1
    assert "no previous build to serve" in proc.stderr


def test_strict_failure_fails_even_with_a_build_on_disk(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    proc = _run(app, "--strict", BUILD_RC="1")
    assert proc.returncode == 1
    assert "(--strict)" in proc.stderr
    assert not _stamp(app).exists()


def test_missing_digested_file_rebuilds_despite_stamp(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    for digested in (app / "priv" / "static" / "assets").iterdir():
        digested.unlink()
    assert _run(app).returncode == 0
    assert _builds(app) == 2


def _lock(app: Path):
    return open(app / "_build" / "assets-build.lock", "a")


def test_held_lock_at_boot_serves_the_build_on_disk(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    with _lock(app) as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = _run(app)
    assert proc.returncode == 0, proc.stderr
    assert "still holds" in proc.stderr
    assert "serving the build already on disk" in proc.stderr
    assert _builds(app) == 1


def test_held_lock_fails_a_strict_build(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    with _lock(app) as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = _run(app, "--strict")
    assert proc.returncode == 1
    assert "still holds" in proc.stderr
    assert _builds(app) == 1


def test_lock_is_released_when_the_build_exits(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert _run(app).returncode == 0
    with _lock(app) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held


def test_lock_is_held_for_the_whole_build(tmp_path: Path) -> None:
    # perl takes the flock and exits; the lock must stay with the script's fd.
    app = _app(tmp_path)
    tmp = app.parent.parent.parent
    env = {
        **os.environ,
        "PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "MIX_ENV": "prod",
        "MIX_CALLS": str(tmp / "mix-calls"),
        "BUILD_SLEEP": "3",
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
        with _lock(app) as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held = True
            else:
                held = False
        assert held, "build lock was not held while the build ran"
    finally:
        assert build.wait(timeout=30) == 0


def test_boot_that_waited_out_another_build_finds_it_current(
    tmp_path: Path,
) -> None:
    # The stamp is checked after the lock is taken, so a boot that queued
    # behind a deploy build does not build again.
    app = _app(tmp_path)
    tmp = app.parent.parent.parent
    (app / "_build").mkdir()
    held = _lock(app)
    fcntl.flock(held, fcntl.LOCK_EX)
    env = {
        **os.environ,
        "PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "MIX_ENV": "prod",
        "MIX_CALLS": str(tmp / "mix-calls"),
        "DIALECTIC_LIVE_ASSETS_LOCK_WAIT": "30",
    }
    boot = subprocess.Popen(
        [str(app / "scripts" / "build-assets.sh")],
        cwd=app,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(1)
        # The "other build" finishes while the boot waits.
        digested = app / "priv" / "static" / "assets" / "app-other.css"
        digested.parent.mkdir(parents=True)
        digested.write_text("css other\n")
        _manifest(app).write_text(
            '{"latest": {"assets/app.css": "assets/app-other.css"}}\n'
        )
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD:elixir/dialectic_live"],
            cwd=app.parent.parent,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        checksum = hashlib.sha256(_manifest(app).read_bytes()).hexdigest()
        (app / "_build" / "assets.stamp").write_text(f"{tree} {checksum}\n")
    finally:
        held.close()
    out, err = boot.communicate(timeout=30)
    assert boot.returncode == 0, err
    assert "build is current" in out
    assert _builds(app) == 0
