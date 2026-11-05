"""An unconfigured Watcher sweep must not inspect a workspace or a backend."""
import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / "agents/watcher/watcher-sweep.sh"


def test_missing_scan_roots_stops_before_state_or_network_calls(tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    calls = tmp_path / "calls"
    for name in ("mkdir", "curl", "pgrep", "touch", "python3"):
        path = bins / name
        path.write_text('#!/bin/sh\nprintf "unexpected\\n" >> "$TEST_CALL_LOG"\nexit 99\n')
        path.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "UNITARES_WATCHER_SWEEP_ROOTS"}
    env.update({"PATH": f"{bins}:{os.environ['PATH']}", "TEST_CALL_LOG": str(calls)})
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "explicit scan roots" in result.stderr
    assert not calls.exists()


def _sweep_env(tmp_path, *, python_exit=0):
    """A sweep with stub tools: a recording interpreter, an unreachable
    Ollama, an empty scan root and an isolated HOME."""
    home = tmp_path / "home"
    (home / "Library" / "Logs").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    bins = tmp_path / "bin"
    bins.mkdir()
    calls = tmp_path / "python-calls"
    interp = bins / "fake-python"
    interp.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit {python_exit}\n')
    interp.chmod(0o755)
    curl = bins / "curl"
    curl.write_text("#!/bin/sh\nexit 7\n")
    curl.chmod(0o755)
    agent = tmp_path / "agent.py"
    agent.write_text("")
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bins}:{os.environ['PATH']}",
            "UNITARES_WATCHER_SWEEP_ROOTS": str(root),
            "UNITARES_WATCHER_AGENT": str(agent),
            "UNITARES_WATCHER_PYTHON": str(interp),
        }
    )
    return env, calls, agent, home / "Library" / "Logs" / "unitares-watcher.log"


def test_lifecycle_runs_every_sweep_even_when_ollama_is_down(tmp_path):
    """Lifecycle needs no model, so it must run before the Ollama skip."""
    env, calls, agent, log = _sweep_env(tmp_path)

    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    assert result.returncode == 0
    assert calls.read_text().splitlines() == [f"{agent} --lifecycle"]
    assert "Ollama unreachable" in log.read_text()


def test_lifecycle_failure_is_logged_with_its_exit_code(tmp_path):
    env, _calls, _agent, log = _sweep_env(tmp_path, python_exit=5)

    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, "a lifecycle failure must not stop the sweep"
    assert "lifecycle failed (exit 5)" in log.read_text()


def test_sweep_scans_real_edits_and_skips_checkout_writes(tmp_path):
    """End to end through the engine: a committed, untouched file whose mtime
    is new (as after a checkout) is skipped; a real edit is scanned."""
    import sys

    env, calls, agent, log = _sweep_env(tmp_path)
    root = Path(env["UNITARES_WATCHER_SWEEP_ROOTS"])
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
               "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
               "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    for args in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=git_env)
    (root / "untouched.py").write_text("a = 1\n")
    (root / "edited.py").write_text("b = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=git_env)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "seed"], check=True, env=git_env)
    (root / "edited.py").write_text("b = 2\n")

    bins = Path(env["PATH"].split(":")[0])
    (bins / "curl").write_text("#!/bin/sh\nexit 0\n")  # Ollama reachable
    (bins / "pgrep").write_text("#!/bin/sh\nexit 1\n")  # no scans running
    (bins / "pgrep").chmod(0o755)
    interp = Path(env["UNITARES_WATCHER_PYTHON"])
    interp.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'case "$*" in\n'
        f'  "-m agents.watcher.sweep_filter"*) exec "{sys.executable}" "$@" ;;\n'
        "esac\n"
        "exit 0\n"
    )

    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    scans = [c for c in calls.read_text().splitlines() if " --all " in f" {c} "]
    assert len(scans) == 1 and scans[0].endswith(f"--file {root / 'edited.py'}"), scans
    assert "1 unedited checkout files skipped" in log.read_text()
