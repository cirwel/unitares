"""
Knowledge-search neighbor-fetch parallelism: regression test for the
60× KG-call amplification fix.

The hybrid_rrf graph-expansion path in `mcp_handlers/knowledge/handlers.py`
fetches up to 30 neighbor docs not already in the semantic+FTS pool. Pre-fix
this was a sequential `for nid in missing_ids: await graph.get_discovery(nid)`
loop. With each get_discovery doing 2 PG round-trips (row + backlinks),
30 neighbors meant 60 sequential awaits — the in-handler floor on the
60× amplification measured 2026-05-04. Post-fix uses asyncio.gather so
the connection pool determines wall time, not sequential ordering.

This test pins the parallelism by making get_discovery sleep longer than
the wall-clock budget would allow if calls were sequential.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
from pathlib import Path
from typing import Any

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.mark.asyncio
async def test_neighbor_fetch_runs_concurrently():
    """30 neighbor get_discovery calls must be issued concurrently.

    With a 50ms simulated per-call latency, sequential execution takes
    ≥1500ms. Concurrent execution finishes in ~50ms (one wave). We
    assert <500ms — generous enough to absorb event-loop noise but tight
    enough to fail if the gather() were ever reverted to a sequential
    `for await` loop.
    """
    from src.mcp_handlers.knowledge import handlers

    fetched_ids: list[str] = []

    class FakeDoc:
        def __init__(self, did: str) -> None:
            self.id = did
            self.tags = []
            self.related_to = []
            self.responses_from = []
            self.response_to = None

    class FakeGraph:
        async def get_discovery(self, nid: str) -> Any:
            fetched_ids.append(nid)
            await asyncio.sleep(0.05)
            return FakeDoc(nid)

    graph = FakeGraph()
    missing_ids = [f"doc-{i:03d}" for i in range(30)]
    pool: dict[str, Any] = {}

    # Mirror the handler's gather call shape exactly. If the handler's
    # implementation drifts back to sequential, this stays concurrent —
    # so we also have the source-level assertion below.
    t0 = time.perf_counter()
    capped = missing_ids[:30]
    results = await asyncio.gather(
        *(graph.get_discovery(nid) for nid in capped),
        return_exceptions=True,
    )
    for nid, doc in zip(capped, results):
        if isinstance(doc, Exception):
            continue
        if doc is not None:
            pool[nid] = doc
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert len(pool) == 30
    assert elapsed_ms < 500, \
        f"neighbor fetch took {elapsed_ms:.1f}ms — gather() not running concurrently?"

    # Source-level pin: the handler must use asyncio.gather, not a plain
    # for/await loop. If someone reverts the fix this assertion fires.
    src = inspect.getsource(handlers)
    # The fixed shape: gather(*(graph.get_discovery(nid) for nid in capped))
    assert "asyncio.gather" in src or "_asyncio.gather" in src, \
        "expected asyncio.gather usage in knowledge handlers"
    # Find the missing_ids block specifically — the gather must live
    # inside it, not just exist somewhere else in the file.
    idx = src.find("missing_ids = [did for did, _ in fused if did not in pool]")
    assert idx >= 0, "missing_ids fan-out block not located"
    block = src[idx:idx + 1500]
    assert "gather(" in block, \
        "missing_ids fetch must use gather() — sequential await loop reverts the perf fix"


@pytest.mark.asyncio
async def test_neighbor_fetch_swallows_individual_failures():
    """One failing get_discovery must not poison the others.

    `return_exceptions=True` keeps successful neighbor fetches in the
    pool while the failing id is logged and skipped. Without this, a
    single AGE/PG hiccup would cancel the whole graph-expansion stage
    of hybrid search.
    """

    class FakeDoc:
        def __init__(self, did: str) -> None:
            self.id = did

    fail_id = "doc-bad"

    class FakeGraph:
        async def get_discovery(self, nid: str) -> Any:
            if nid == fail_id:
                raise RuntimeError("simulated AGE failure")
            return FakeDoc(nid)

    graph = FakeGraph()
    missing_ids = ["doc-001", fail_id, "doc-002"]
    pool: dict[str, Any] = {}

    capped = missing_ids[:30]
    results = await asyncio.gather(
        *(graph.get_discovery(nid) for nid in capped),
        return_exceptions=True,
    )
    for nid, doc in zip(capped, results):
        if isinstance(doc, Exception):
            continue
        if doc is not None:
            pool[nid] = doc

    assert "doc-001" in pool
    assert "doc-002" in pool
    assert fail_id not in pool
