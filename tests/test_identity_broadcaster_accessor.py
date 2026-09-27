"""The identity surface's broadcaster accessors reach the real broadcaster.

``handlers._broadcaster`` and ``persistence._broadcaster`` imported a name
``src.broadcaster`` never defined (``broadcaster``; the singleton is
``broadcaster_instance``). The ImportError was swallowed, both accessors
returned None, and every identity event they gate was skipped:
``identity_hijack_suspected`` (PATH 0, PATH 1, PATH 2, the middleware and the
sync fingerprint check), ``pg_session_collision`` and
``resident_fork_detected``. Every emission test patched the accessor with a
mock, so none of them saw it. These tests call the accessors unpatched.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import src.broadcaster as broadcaster_module
from src.broadcaster import broadcaster_instance
from src.mcp_handlers.identity import handlers, persistence

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


def test_handlers_accessor_returns_the_shared_broadcaster():
    assert handlers._broadcaster() is broadcaster_instance


def test_persistence_accessor_returns_the_shared_broadcaster():
    assert persistence._broadcaster() is broadcaster_instance


def _broadcaster_imports():
    """Every ``from src.broadcaster import <name>`` under src/, as (file, line, name)."""
    found = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "src.broadcaster":
                for alias in node.names:
                    found.append((path.relative_to(SRC_ROOT.parent), node.lineno, alias.name))
    return found


def test_every_src_broadcaster_import_names_a_real_attribute():
    """The accessors swallow ImportError, so a wrong name fails silently at
    runtime. Hold every import of the module to a name it defines."""
    imports = _broadcaster_imports()
    assert imports, "expected at least one `from src.broadcaster import ...` under src/"
    missing = [
        f"{path}:{line} imports {name!r}"
        for path, line, name in imports
        if name != "*" and not hasattr(broadcaster_module, name)
    ]
    assert not missing, "src.broadcaster has no such attribute: " + "; ".join(missing)


@pytest.mark.asyncio
async def test_hijack_event_reaches_the_shared_broadcaster():
    """PATH 0's hijack event, emitted through the unpatched accessor."""
    uuid = "5b2f0c1e-9a4d-4c3b-8e21-7d6f5a4b3c2d"
    with patch.object(broadcaster_instance, "broadcast_event", new=AsyncMock()) as sent:
        await handlers._emit_identity_hijack_event(uuid, "log", None)

    sent.assert_awaited_once()
    kwargs = sent.await_args.kwargs
    assert kwargs["event_type"] == "identity_hijack_suspected"
    assert kwargs["agent_id"] == uuid
    assert kwargs["payload"]["proof"] == "none"


@pytest.mark.asyncio
async def test_resident_fork_event_reaches_the_shared_broadcaster():
    """An unlineaged collision on a persistent label, emitted through the
    unpatched persistence accessor."""
    existing_uuid = "907e3195-c649-49db-b753-1edc1a105f33"
    new_uuid = "7bf970d4-5713-4184-a6f8-58e798275f3f"

    mock_db = AsyncMock()
    mock_db.find_agent_by_label = AsyncMock(return_value=existing_uuid)
    mock_db.agent_has_tag = AsyncMock(return_value=True)
    mock_db.update_agent_fields = AsyncMock(return_value=True)
    mock_db.get_identity = AsyncMock(return_value=None)

    with patch.object(persistence, "get_db", return_value=mock_db), \
         patch.object(persistence, "mcp_server", SimpleNamespace(agent_metadata={})), \
         patch.object(broadcaster_instance, "broadcast_event", new=AsyncMock()) as sent:
        await persistence.set_agent_label(new_uuid, "Watcher", session_key="sk")

    sent.assert_awaited_once()
    kwargs = sent.await_args.kwargs
    assert kwargs["event_type"] == "resident_fork_detected"
    assert kwargs["agent_id"] == new_uuid
    assert kwargs["payload"]["existing_agent_id"] == existing_uuid


@pytest.mark.asyncio
async def test_sync_fingerprint_event_is_tracked_and_reaches_the_shared_broadcaster(monkeypatch):
    """The sync PATH 1 fingerprint check cannot await, so it schedules the
    broadcast. The task must be held (create_tracked_task), not a bare
    loop.create_task the loop references only weakly."""
    import src.background_tasks as background_tasks
    from src.mcp_handlers.identity import shared

    key = "agent-5b2f0c1e9a4d"
    uuid = "5b2f0c1e-9a4d-4c3b-8e21-7d6f5a4b3c2d"
    monkeypatch.setitem(shared._bind_fingerprints, key, "fp_bound")

    tracked = []
    create = background_tasks.create_tracked_task

    def _track(coro, *, name=None):
        task = create(coro, name=name)
        tracked.append(task)
        return task

    monkeypatch.setattr(background_tasks, "create_tracked_task", _track)

    with patch.object(shared, "get_session_signals",
                      return_value=SimpleNamespace(ip_ua_fingerprint="fp_other")), \
         patch.object(shared, "session_fingerprint_check_mode", return_value="log"), \
         patch.object(broadcaster_instance, "broadcast_event", new=AsyncMock()) as sent:
        assert shared._check_path1_fingerprint_sync(key, uuid) is True
        assert len(tracked) == 1, "the broadcast must be scheduled through create_tracked_task"
        await tracked[0]

    sent.assert_awaited_once()
    kwargs = sent.await_args.kwargs
    assert kwargs["event_type"] == "identity_hijack_suspected"
    assert kwargs["agent_id"] == uuid
    assert kwargs["payload"]["path"] == "path1_sync_session_id"
