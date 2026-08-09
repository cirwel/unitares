"""Regression test: _post_resolution_event must use an event_type the
governance /api/findings endpoint actually accepts. The HTTP layer rejects
any type not ending in '_finding' (src/http_api.py:1090). The original
implementation posted 'watcher_resolution' and got silently 400'd."""

import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import agents.watcher.agent as watcher_agent
from agents.watcher.agent import _post_resolution_event


def test_resolution_event_type_passes_findings_suffix_gate():
    """The event_type Watcher posts must end in '_finding' so the
    /api/findings suffix gate accepts it. Without this, every confirm/dismiss
    is silently dropped."""
    finding = {
        "fingerprint": "abcd1234efgh5678",
        "pattern": "P-DUMMY",
        "file": "/tmp/x.py",
        "line": 1,
        "hint": "h",
        "severity": "medium",
        "violation_class": "BEH",
    }
    captured = {}

    def fake_post_finding(*, event_type, **kwargs):
        captured["event_type"] = event_type
        captured["kwargs"] = kwargs
        return True

    # Stub get_watcher_identity so the function actually proceeds to post_finding
    fake_identity = {
        "agent_uuid": "11111111-2222-3333-4444-555555555555",
        "client_session_id": "csid",
        "continuity_token": "tok",
    }
    with patch("agents.watcher.agent.get_watcher_identity", return_value=fake_identity):
        with patch("agents.watcher.agent.post_finding", side_effect=fake_post_finding):
            _post_resolution_event(finding, "confirmed", "agent-uuid", reason="fp")

    assert "event_type" in captured, "post_finding was not called"
    assert captured["event_type"].endswith("_finding"), (
        f"event_type {captured['event_type']!r} would be 400'd by the "
        "/api/findings suffix gate"
    )


def test_script_entry_runs_the_package_modules_main(monkeypatch):
    """Executing agent.py as a script must run the PACKAGE module's main().

    Same silent-drop family as the test above, one layer down. Running the file
    as a script (``python3 agents/watcher/agent.py --dismiss …`` — the form the
    SessionStart banner prints) binds it as ``__main__``, a module object
    distinct from ``agents.watcher.agent``. findings.py reaches
    _post_resolution_event through a lazy ``from agents.watcher.agent import``,
    so module-level state that main() sets — notably ``_watcher_identity`` —
    landed on the ``__main__`` copy and was invisible to the copy that posts.
    Every operator --resolve/--dismiss skipped its governance post, and
    ``watcher_resolution_finding`` sat at zero rows for the producer's whole
    life while the CLI still printed ``ok:``.

    The tests above import the module normally, so they never take that path —
    which is exactly why they stayed green. This one drives the real entry.
    """
    called = {}

    def _recording_main(argv=None):
        called["ran"] = True
        return 0

    monkeypatch.setattr(watcher_agent, "main", _recording_main)
    monkeypatch.setattr(sys, "argv", ["agent.py", "--list-findings"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(Path(watcher_agent.__file__)), run_name="__main__")

    assert exc_info.value.code == 0
    assert called.get("ran"), (
        "agent.py ran its own __main__ copy of main() instead of the package "
        "module's — identity set there is invisible to findings.py's lazy "
        "import, silently disabling resolution-event posting"
    )


def test_missing_identity_is_logged_not_silent():
    """An unresolved identity must leave a trace, not vanish.

    The silent ``return`` here is what made the entry-form bug above invisible
    for the producer's entire life: --dismiss printed ``ok:`` and nothing
    anywhere recorded that the governance post had been skipped.
    """
    finding = {
        "fingerprint": "abcd1234efgh5678",
        "pattern": "P-DUMMY",
        "file": "/tmp/x.py",
        "line": 1,
        "hint": "h",
        "severity": "medium",
        "violation_class": "BEH",
    }
    logged: list[tuple[str, str]] = []

    with patch("agents.watcher.agent.get_watcher_identity", return_value=None):
        with patch(
            "agents.watcher.agent.log",
            side_effect=lambda msg, level="info": logged.append((msg, level)),
        ):
            _post_resolution_event(finding, "dismissed", "agent-uuid")

    assert logged, "skipping the resolution post left no log line at all"
    msg, level = logged[-1]
    assert level == "warning"
    assert "identity" in msg.lower()
