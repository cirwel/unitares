"""Contract tests for dialectic_live's boot/deploy asset build.

`elixir/dialectic_live/scripts/build-assets.sh` skips the build when the
build on disk is the one for this checkout, builds otherwise, and on a failed
build either falls back to what is on disk (lenient, every boot) or fails
(--strict, deploys). These tests run the real script inside a scratch git
checkout with `mix` and the CLI-repair helper stubbed. Like phx.digest, the
`mix` stub rewrites the manifest with new content on every build.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "elixir" / "dialectic_live" / "scripts" / "build-assets.sh"

MIX_STUB = """#!/usr/bin/env bash
set -eu
echo "$*" >> "$MIX_CALLS"
case "$1" in
  assets.deploy)
    [ "${BUILD_RC:-0}" -eq 0 ] || exit "$BUILD_RC"
    mkdir -p priv/static
    echo "{\\"digest\\": \\"$(date +%s)-$$-$RANDOM\\"}" > priv/static/cache_manifest.json
    ;;
  *) echo "unexpected mix $*" >&2; exit 99 ;;
esac
"""

GITIGNORE = "/_build/\n/priv/static/cache_manifest.json\n"


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
