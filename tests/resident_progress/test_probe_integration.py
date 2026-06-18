from __future__ import annotations

import asyncio

import pytest

from src import background_tasks


@pytest.mark.asyncio
async def test_progress_flat_probe_task_is_supervised_when_started(monkeypatch):
    """Confirm start_all_background_tasks creates a supervised task named
    'progress_flat_probe'. Other background tasks are also created — we
    do not assert their absence, only that ours is present.
    """
    background_tasks._supervised_tasks.clear()

    async def fake_set_ready():
        pass

    background_tasks.start_all_background_tasks(set_ready=fake_set_ready)
    names = [t.get_name() if hasattr(t, "get_name") else (getattr(t, "_name", "") or "") for t in background_tasks._supervised_tasks]
    assert any("progress_flat_probe" in n for n in names), \
        f"progress_flat_probe not in supervised task list: {names}"
    await background_tasks.stop_all_background_tasks()
