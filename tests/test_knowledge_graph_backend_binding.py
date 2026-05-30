"""Regression tests for knowledge graph backend factory binding."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_age_backend_resolves_db_factory_at_call_time(monkeypatch):
    """AGE backend must not keep a stale import-time get_db binding."""
    import src.db as db_module
    import src.storage.knowledge_graph_age as age_module

    stale_db = MagicMock(name="stale_db")
    stale_db.init = AsyncMock()
    stale_db.graph_available = AsyncMock(return_value=False)

    current_db = MagicMock(name="current_db")
    current_db.init = AsyncMock()
    current_db.graph_available = AsyncMock(return_value=False)

    monkeypatch.setattr(age_module, "get_db", lambda: stale_db, raising=False)
    monkeypatch.setattr(db_module, "get_db", lambda: current_db)

    kg = age_module.KnowledgeGraphAGE()
    db = await kg._get_db()

    assert db is current_db
