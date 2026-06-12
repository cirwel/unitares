"""Tests for the region-aware hook input helper.

Background: the PostToolUse hook (agents/watcher/watcher-hook.sh) previously
passed only --file to the watcher, so the model scanned the whole file even
for a two-line edit. On a 1951-line file the default 90s timeout was not
enough. The hook_input module uses ``git diff --unified=<context>`` to
extract just the regions that changed, so the watcher sees ~60-150 lines
instead of the full file.

Design notes:
- ``git diff --unified=5`` is the de-facto primitive used by pre-commit,
  reviewdog, CodeRabbit and Sourcery. The 5-line surrounding context is
  what LLM review needs for semantic reasoning (bare diff lines alone are
  too local).
- Hunk headers ``@@ -a,b +c,d @@`` give us the new-side (c, c+d-1) range
  directly; no old_string location search needed.
- ``merge_adjacent`` addresses the review concern that a naive bounding box
  over two far-apart edits (e.g. line 10 and line 900) would pass an
  ~900-line prompt — as useless as scanning the whole file. Clusters with
  small gaps merge (one prompt); disjoint clusters stay separate (two
  sequential prompts).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def hook_input_module():
    """Load agents/watcher/hook_input.py as a module without running __main__."""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    module_path = project_root / "agents" / "watcher" / "hook_input.py"
    spec = importlib.util.spec_from_file_location("watcher_hook_input", module_path)
    assert spec and spec.loader, f"could not load {module_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["watcher_hook_input"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# extract_regions — parses git diff output into (start, end) tuples
# ---------------------------------------------------------------------------


def _stub_git_diff(monkeypatch, module, stdout_text: str, returncode: int = 0):
    """Replace the module's subprocess.run with a stub returning the given stdout."""
    def _fake_run(cmd, capture_output=True, text=True, timeout=None, cwd=None):
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout_text, stderr=""
        )
    monkeypatch.setattr(module.subprocess, "run", _fake_run)


def test_extract_regions_single_hunk(hook_input_module, monkeypatch):
    """One @@ header → one (start, end) tuple using the new-side numbers.

    Header ``@@ -10,15 +12,20 @@`` means the new file adds 20 lines starting
    at line 12, so the region covers lines 12 through 31 inclusive."""
    diff_output = (
        "diff --git a/foo.py b/foo.py\n"
        "index abc..def 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -10,15 +12,20 @@ def something():\n"
        " context\n"
        "-removed\n"
        "+added\n"
    )
    _stub_git_diff(monkeypatch, hook_input_module, diff_output)

    regions = hook_input_module.extract_regions("/tmp/foo.py")

    assert regions == [(12, 31)]


def test_extract_regions_multi_hunk(hook_input_module, monkeypatch):
    """Two @@ headers → two distinct regions preserved in order."""
    diff_output = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -10,5 +10,7 @@\n"
        " context\n"
        "+added\n"
        "@@ -100,3 +102,8 @@\n"
        " context\n"
        "+added\n"
    )
    _stub_git_diff(monkeypatch, hook_input_module, diff_output)

    regions = hook_input_module.extract_regions("/tmp/foo.py")

    assert regions == [(10, 16), (102, 109)]


def test_extract_regions_empty_diff_returns_empty_list(hook_input_module, monkeypatch):
    """No changes → git prints nothing → no regions. Caller falls back to
    scanning the whole file (or skipping, its choice)."""
    _stub_git_diff(monkeypatch, hook_input_module, "")

    regions = hook_input_module.extract_regions("/tmp/foo.py")

    assert regions == []


def test_extract_regions_untracked_file_returns_empty_list(
    hook_input_module, monkeypatch
):
    """Untracked file → git diff returns non-zero and an error on stderr.
    Must not raise — the hook is best-effort, fall back to full-file scan."""
    _stub_git_diff(monkeypatch, hook_input_module, "", returncode=128)

    regions = hook_input_module.extract_regions("/tmp/foo.py")

    assert regions == []


def test_extract_regions_handles_single_line_hunk(hook_input_module, monkeypatch):
    """``@@ -42 +42 @@`` (no comma) means one-line old, one-line new at line 42."""
    diff_output = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -42 +42 @@\n"
        "-old\n"
        "+new\n"
    )
    _stub_git_diff(monkeypatch, hook_input_module, diff_output)

    regions = hook_input_module.extract_regions("/tmp/foo.py")

    assert regions == [(42, 42)]


def test_extract_regions_subprocess_timeout_returns_empty_list(
    hook_input_module, monkeypatch
):
    """Git itself hanging → extraction must time out and return empty, not
    propagate. Hook must never block the editor."""
    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)
    monkeypatch.setattr(hook_input_module.subprocess, "run", _raise_timeout)

    regions = hook_input_module.extract_regions("/tmp/foo.py")

    assert regions == []


def test_extract_regions_works_against_real_repo(hook_input_module, tmp_path):
    """Regression guard for the cwd-vs-relative-path bug: git must see the
    file regardless of where the caller invoked extract_regions from.

    The first version of this module set cwd to the file's parent but passed
    the relative file_path, so git resolved the path under the wrong root
    and returned empty — the hook silently fell back to full-file scans.
    """
    import os
    # Minimal real git repo with one committed file and an unstaged edit.
    repo = tmp_path / "repo"
    repo.mkdir()
    original_cwd = os.getcwd()
    try:
        os.chdir(repo)
        subprocess.run(["git", "init", "-q"], check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "config", "user.name", "t"], check=True)
        # Hermetic: host config may force commit signing (e.g. remote containers)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], check=True)
        target = repo / "foo.py"
        target.write_text("\n".join(f"line{i}" for i in range(1, 51)) + "\n")
        subprocess.run(["git", "add", "foo.py"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], check=True)
        # Edit one line in the middle.
        content = target.read_text().splitlines()
        content[24] = "line25_CHANGED"
        target.write_text("\n".join(content) + "\n")

        # Call with an absolute path from a cwd that is not the repo root.
        os.chdir(tmp_path)
        regions = hook_input_module.extract_regions(str(target))
    finally:
        os.chdir(original_cwd)

    assert regions, "extract_regions must find the edit regardless of caller cwd"
    start, end = regions[0]
    assert start <= 25 <= end, f"region {regions[0]} must cover the edited line 25"


# ---------------------------------------------------------------------------
# merge_adjacent — collapse near clusters, keep disjoint ranges separate
# ---------------------------------------------------------------------------


def test_merge_adjacent_collapses_nearby_regions(hook_input_module):
    """Two regions with a small gap must merge into one bounding range so
    the watcher fires one prompt, not two."""
    regions = [(10, 30), (45, 70)]  # gap of 14 lines

    merged = hook_input_module.merge_adjacent(regions, gap=50)

    assert merged == [(10, 70)]


def test_merge_adjacent_keeps_disjoint_clusters(hook_input_module):
    """Regions with a large gap must stay separate so the prompt per scan
    stays small. The watcher fires one scan per disjoint cluster."""
    regions = [(10, 30), (900, 920)]  # gap of 869 lines

    merged = hook_input_module.merge_adjacent(regions, gap=50)

    assert merged == [(10, 30), (900, 920)]


def test_merge_adjacent_chains_three_nearby_regions(hook_input_module):
    """Chain of close regions all fold into one span."""
    regions = [(10, 20), (25, 40), (45, 60)]

    merged = hook_input_module.merge_adjacent(regions, gap=10)

    assert merged == [(10, 60)]


def test_merge_adjacent_respects_gap_boundary(hook_input_module):
    """Gap exactly equal to the threshold should merge (gap is 'at most')."""
    regions = [(10, 30), (80, 100)]  # gap of 49

    merged = hook_input_module.merge_adjacent(regions, gap=50)

    assert merged == [(10, 100)]


def test_merge_adjacent_empty_input_is_empty(hook_input_module):
    """No regions in → no regions out. Caller treats empty list as
    'scan whole file' or 'skip entirely'; this function doesn't decide."""
    assert hook_input_module.merge_adjacent([], gap=50) == []


def test_merge_adjacent_single_region_passes_through(hook_input_module):
    """One region in → same region out."""
    assert hook_input_module.merge_adjacent([(10, 30)], gap=50) == [(10, 30)]


# ---------------------------------------------------------------------------
# format_region — render (start, end) as the watcher's --region flag value
# ---------------------------------------------------------------------------


def test_format_region_matches_watcher_region_syntax(hook_input_module):
    """Output must be the exact form agent.py's --region argument expects:
    ``L<start>-L<end>``. If this drifts, the watcher silently ignores the
    flag and falls back to scanning head."""
    assert hook_input_module.format_region((42, 58)) == "L42-L58"
