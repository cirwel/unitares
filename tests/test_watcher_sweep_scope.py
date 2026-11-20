"""An unconfigured Watcher sweep must not inspect a workspace or a backend."""
import os
from pathlib import Path
import subprocess
import time

import pytest

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


@pytest.mark.parametrize("relative_root", [False, True])
def test_sweep_scans_real_edits_and_skips_checkout_writes(tmp_path, relative_root):
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

    # Pre-set the watermark an hour back: the engine's own seeding uses BSD
    # `date -v`, which fails on Linux CI and would put the watermark after
    # the files above.
    state = Path(env["HOME"]) / ".unitares" / "watcher-sweep.state"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.touch()
    hour_ago = time.time() - 3600
    os.utime(state, (hour_ago, hour_ago))

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

    cwd = None
    if relative_root:
        # Review finding on #3205 (codex, P2): run from the root's parent with
        # a relative root, as an operator might by hand.
        cwd = root.parent
        env["UNITARES_WATCHER_SWEEP_ROOTS"] = root.name
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True,
                            text=True, cwd=cwd)

    assert result.returncode == 0, result.stderr
    scans = [c for c in calls.read_text().splitlines() if c.startswith(f"{agent} --file ")]
    assert scans == [f"{agent} --file {root / 'edited.py'}"], scans
    assert "1 unedited checkout files skipped" in log.read_text()


def _scanning_env(tmp_path):
    """A sweep that reaches the scan loop: Ollama answers, no scans are
    running, the edit filter is the real one, and the scan root is not a git
    work tree, so every file in it counts as an edit."""
    import sys

    env, calls, agent, log = _sweep_env(tmp_path)
    root = Path(env["UNITARES_WATCHER_SWEEP_ROOTS"])
    state = Path(env["HOME"]) / ".unitares" / "watcher-sweep.state"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.touch()
    hour_ago = time.time() - 3600
    os.utime(state, (hour_ago, hour_ago))
    bins = Path(env["PATH"].split(":")[0])
    (bins / "curl").write_text("#!/bin/sh\nexit 0\n")
    (bins / "pgrep").write_text("#!/bin/sh\nexit 1\n")
    (bins / "pgrep").chmod(0o755)
    Path(env["UNITARES_WATCHER_PYTHON"]).write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'case "$*" in\n'
        f'  "-m agents.watcher.sweep_filter"*) exec "{sys.executable}" "$@" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    return env, calls, agent, log, root, state


def _scan_calls(calls, agent):
    return [c for c in calls.read_text().splitlines() if c.startswith(f"{agent} --file ")]


def test_sweep_runs_the_pattern_scan_only_by_default(tmp_path):
    """Review findings come from no rule and nothing calibrates them, so an
    unattended sweep does not produce them unless asked to."""
    env, calls, agent, _log, root, _state = _scanning_env(tmp_path)
    (root / "a.py").write_text("a = 1\n")

    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert _scan_calls(calls, agent) == [f"{agent} --file {root / 'a.py'}"]


def test_sweep_runs_the_review_scan_when_opted_in(tmp_path):
    env, calls, agent, _log, root, _state = _scanning_env(tmp_path)
    env["UNITARES_WATCHER_SWEEP_REVIEW"] = "1"
    (root / "a.py").write_text("a = 1\n")

    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert _scan_calls(calls, agent) == [f"{agent} --file {root / 'a.py'} --all"]


def test_many_candidates_do_not_stall_the_sweep(tmp_path):
    """The emptiness test on the candidate list was a bash pattern
    substitution, quadratic in bash 3.2 (macOS /bin/bash): 34 seconds on 100
    paths, and a sweep with 1,500 sat in it for 22 hours. 400 paths took
    minutes; the count that replaces it takes none."""
    env, calls, agent, log, root, _state = _scanning_env(tmp_path)
    env["UNITARES_WATCHER_SWEEP_MAX"] = "2"
    for i in range(400):
        (root / f"module_with_a_longish_name_{i:04d}.py").write_text("a = 1\n")

    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True,
                            text=True, timeout=120)

    assert result.returncode == 0, result.stderr
    assert len(_scan_calls(calls, agent)) == 2
    assert "400 changed, scanned 2 (cap 2 hit" in log.read_text()


def test_a_watermark_older_than_the_window_is_moved_up(tmp_path):
    """A held watermark only falls further behind when edits outrun the cap.
    Past the window it is moved up, so the sweep stays a backstop for recent
    edits and the candidate list stays bounded."""
    env, _calls, _agent, log, _root, state = _scanning_env(tmp_path)
    env["UNITARES_WATCHER_SWEEP_MAX_WINDOW_H"] = "6"
    long_ago = time.time() - 14 * 86400
    os.utime(state, (long_ago, long_ago))
    # Ollama down: the run stops before it would advance the watermark itself.
    Path(env["PATH"].split(":")[0], "curl").write_text("#!/bin/sh\nexit 7\n")

    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    age_h = (time.time() - state.stat().st_mtime) / 3600
    assert 5.5 < age_h < 6.5, age_h
    assert "watermark was more than 6h old" in log.read_text()


def test_a_watermark_inside_the_window_is_left_alone(tmp_path):
    env, _calls, _agent, log, _root, state = _scanning_env(tmp_path)
    before = state.stat().st_mtime
    Path(env["PATH"].split(":")[0], "curl").write_text("#!/bin/sh\nexit 7\n")

    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert state.stat().st_mtime == before
    assert "watermark was more than" not in log.read_text()


def test_a_capped_sweep_resumes_where_the_last_one_stopped(tmp_path):
    """While the watermark is held, each sweep sees the same candidates. The
    hook deletes its locks after ten minutes, so they cannot carry "already
    scanned" from one sweep to the next; without its own record the sweep
    scanned the same first files every interval and never reached the rest."""
    env, calls, agent, log, root, state = _scanning_env(tmp_path)
    env["UNITARES_WATCHER_SWEEP_MAX"] = "2"
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
        (root / name).write_text("a = 1\n")
    watermark = state.stat().st_mtime

    def sweep():
        result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        # What the hook does between sweeps: its locks do not outlive ten minutes.
        for lock in Path("/tmp/unitares-watcher-locks").glob("*.lock"):
            if lock.stat().st_mtime >= watermark:
                lock.unlink(missing_ok=True)

    sweep()
    sweep()
    assert state.stat().st_mtime == watermark, "watermark held while the cap is hit"
    sweep()

    scanned = [c.split(" --file ")[1] for c in _scan_calls(calls, agent)]
    assert sorted(scanned) == sorted(str(root / n) for n in ("a.py", "b.py", "c.py", "d.py", "e.py")), scanned
    assert state.stat().st_mtime > watermark, "a pass that finishes under the cap advances it"
    assert not (state.parent / "watcher-sweep.done").exists()
    assert "2 already scanned in this window" in log.read_text()


def test_a_file_edited_after_its_scan_is_scanned_again_in_the_same_window(tmp_path):
    env, calls, agent, _log, root, _state = _scanning_env(tmp_path)
    env["UNITARES_WATCHER_SWEEP_MAX"] = "1"
    (root / "a.py").write_text("a = 1\n")
    (root / "b.py").write_text("b = 1\n")

    subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=True)
    first = _scan_calls(calls, agent)
    assert len(first) == 1
    scanned_first = Path(first[0].split(" --file ")[1])
    time.sleep(1.1)  # past the marker's mtime at one-second resolution
    scanned_first.write_text("a = 2\n")
    for lock in Path("/tmp/unitares-watcher-locks").glob("*.lock"):
        lock.unlink(missing_ok=True)

    subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=True)
    subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=True)

    scanned = [c.split(" --file ")[1] for c in _scan_calls(calls, agent)]
    assert scanned.count(str(scanned_first)) == 2, scanned


def test_hook_and_sweep_count_the_same_scans():
    """Review finding on this change (codex, P2): the sweep's pattern-only
    scans start `agent.py --file`, which the hook's `agent.py --all` matcher
    did not count, so the two could exceed the shared concurrency limit."""
    import re

    hook = SCRIPT.with_name("watcher-hook.sh").read_text()
    sweep = SCRIPT.read_text()
    matcher = re.compile(r'pgrep -f "([^"]+)"')
    hook_patterns = matcher.findall(hook)
    sweep_patterns = matcher.findall(sweep)
    assert hook_patterns == sweep_patterns == ["agents/watcher/agent.py --(all|file) "]
    counted = re.compile(hook_patterns[0])
    for command in (
        "python3 /repo/agents/watcher/agent.py --file /x/a.py",
        "python3 /repo/agents/watcher/agent.py --file /x/a.py --region L1-L40",
        "python3 /repo/agents/watcher/agent.py --file /x/a.py --all",
        "python3 /repo/agents/watcher/agent.py --all --file /x/a.py",
    ):
        assert counted.search(command), command
    assert not counted.search("python3 /repo/agents/watcher/agent.py --lifecycle")
