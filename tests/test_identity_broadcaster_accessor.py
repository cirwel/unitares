"""The identity surface's broadcaster accessors reach the real broadcaster.

``handlers._broadcaster`` and ``persistence._broadcaster`` imported a name
``src.broadcaster`` never defined (``broadcaster``; the singleton is
``broadcaster_instance``). The ImportError was swallowed, both accessors
returned None, and every identity event they gate was skipped:
``identity_hijack_suspected`` (PATH 0, PATH 1, PATH 2, the middleware and the
sync fingerprint check), ``pg_session_collision`` and
``resident_fork_detected``. Every emission test patched the accessor with a
mock, so none of them saw it. These tests call the accessors unpatched.

The accessors hand out a ``_ScheduledBroadcaster``: the event is scheduled as
a tracked task, so identity resolution never waits on the WebSocket fan-out.
"""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import src.background_tasks as background_tasks
import src.broadcaster as broadcaster_module
from src.broadcaster import broadcaster_instance
from src.mcp_handlers.identity import handlers, persistence, resolution, shared

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
UUID = "5b2f0c1e-9a4d-4c3b-8e21-7d6f5a4b3c2d"


@pytest.fixture
def tracked(monkeypatch):
    """Record every task scheduled through create_tracked_task."""
    tasks = []
    create = background_tasks.create_tracked_task

    def _track(coro, *, name=None):
        task = create(coro, name=name)
        tasks.append(task)
        return task

    monkeypatch.setattr(background_tasks, "create_tracked_task", _track)
    return tasks


async def _drain(tasks):
    """Await scheduled tasks, including ones they schedule in turn."""
    while any(not t.done() for t in tasks):
        await asyncio.gather(*[t for t in tasks if not t.done()])


def test_handlers_accessor_wraps_the_shared_broadcaster():
    b = handlers._broadcaster()
    assert isinstance(b, persistence._ScheduledBroadcaster)
    assert b._target is broadcaster_instance


def test_persistence_accessor_wraps_the_shared_broadcaster():
    b = persistence._broadcaster()
    assert isinstance(b, persistence._ScheduledBroadcaster)
    assert b._target is broadcaster_instance


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
async def test_hijack_event_reaches_the_shared_broadcaster(tracked):
    """PATH 0's hijack event, emitted through the unpatched accessor."""
    with patch.object(broadcaster_instance, "broadcast_event", new=AsyncMock()) as sent:
        await handlers._emit_identity_hijack_event(UUID, "log", None)
        await _drain(tracked)

    sent.assert_awaited_once()
    kwargs = sent.await_args.kwargs
    assert kwargs["event_type"] == "identity_hijack_suspected"
    assert kwargs["agent_id"] == UUID
    assert kwargs["payload"]["proof"] == "none"


@pytest.mark.asyncio
async def test_resident_fork_event_reaches_the_shared_broadcaster(tracked):
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
        await _drain(tracked)

    sent.assert_awaited_once()
    kwargs = sent.await_args.kwargs
    assert kwargs["event_type"] == "resident_fork_detected"
    assert kwargs["agent_id"] == new_uuid
    assert kwargs["payload"]["existing_agent_id"] == existing_uuid


@pytest.mark.asyncio
async def test_sync_fingerprint_event_is_tracked_and_reaches_the_shared_broadcaster(
    monkeypatch, tracked
):
    """The sync PATH 1 fingerprint check cannot await, so it schedules the
    broadcast. The task must be held (create_tracked_task), not a bare
    loop.create_task the loop references only weakly."""
    key = "agent-5b2f0c1e9a4d"
    monkeypatch.setitem(shared._bind_fingerprints, key, "fp_bound")

    with patch.object(shared, "get_session_signals",
                      return_value=SimpleNamespace(ip_ua_fingerprint="fp_other")), \
         patch.object(shared, "session_fingerprint_check_mode", return_value="log"), \
         patch.object(broadcaster_instance, "broadcast_event", new=AsyncMock()) as sent:
        assert shared._check_path1_fingerprint_sync(key, UUID) is True
        assert tracked, "the broadcast must be scheduled through create_tracked_task"
        await _drain(tracked)

    sent.assert_awaited_once()
    kwargs = sent.await_args.kwargs
    assert kwargs["event_type"] == "identity_hijack_suspected"
    assert kwargs["agent_id"] == UUID
    assert kwargs["payload"]["path"] == "path1_sync_session_id"


@pytest.mark.asyncio
async def test_a_stalled_fanout_does_not_hold_identity_resolution(tracked):
    """The REST csid corroboration lookup bounds resolution at 0.5 s
    (http_routes/access.py). A PATH 1 fingerprint mismatch inside it must not
    wait for a broadcast held up by a stalled WebSocket client."""
    release = asyncio.Event()
    delivered = []

    async def _stalled_broadcast(**kwargs):
        await release.wait()
        delivered.append(kwargs)

    sig = SimpleNamespace(ip_ua_fingerprint="fp_other")
    with patch("config.governance_config.session_fingerprint_check_mode", return_value="log"), \
         patch("config.governance_config.prefix_bind_fingerprint_mode", return_value="off"), \
         patch("src.mcp_handlers.context.get_session_signals", return_value=sig), \
         patch.object(broadcaster_instance, "broadcast_event", new=_stalled_broadcast):
        blocked = await asyncio.wait_for(
            resolution._fingerprint_hijack_check("1.2.3.4:ua", "fp_bound", UUID),
            timeout=0.5,
        )
        assert blocked is False  # log mode: resolution proceeds
        assert not delivered  # the fan-out is still pending
        release.set()
        await _drain(tracked)

    assert len(delivered) == 1
    assert delivered[0]["event_type"] == "identity_hijack_suspected"
    assert delivered[0]["payload"]["reason"] == "fingerprint_mismatch"
