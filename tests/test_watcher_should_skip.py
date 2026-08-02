"""Path-scope guards for the Watcher's should_skip().

Watcher findings persist until a human (or agent) resolves or dismisses
them, so scanning ephemeral locations mints permanent tracking debt for
throwaway files. Canonical instance: P012 #7b0af90b (2026-08-01) flagged
an unvalidated parse in a Claude Code session scratchpad
(/private/tmp/claude-501/.../scratchpad/render_preamble.py) — technically
true, permanently unactionable, hand-dismissed as out_of_scope.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from agents.watcher.agent import should_skip


def test_session_scratchpad_paths_skipped():
    """Files under /tmp/claude-<anything> are never scanned. On macOS /tmp
    resolves to /private/tmp, so the fragment must match the resolved form
    too — substring matching covers both."""
    d = tempfile.mkdtemp(prefix="claude-", dir="/tmp")
    try:
        f = Path(d) / "scratchpad" / "render_preamble.py"
        f.parent.mkdir(parents=True)
        f.write_text("import json\ndata = json.loads(open('x').read())\n")
        skip, reason = should_skip(str(f))
        assert skip is True
        assert "/tmp/claude-" in reason
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ordinary_tmp_file_not_swept_by_scratchpad_fragment(tmp_path):
    """The exclusion is scoped to claude-* session dirs, not all of /tmp —
    pytest's tmp_path (private var folder) must stay scannable."""
    f = tmp_path / "example.py"
    f.write_text("x = 1\n")
    skip, reason = should_skip(str(f))
    assert skip is False
    assert reason == ""
