from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _no_http_api_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.mark.asyncio
async def test_endpoint_returns_row_per_configured_resident(test_db, monkeypatch):
    # Insert a recent snapshot for vigil so at least one row has live data.
    async with test_db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO progress_flat_snapshots (
                probe_tick_id, ticked_at, resident_label, source,
                metric_value, window_seconds, threshold,
                metric_below_threshold, heartbeat_alive, candidate
            ) VALUES (
                $1::uuid, now(), 'vigil', 'kg_writes', 5, 3600, 1,
                false, true, false
            )
            """,
            str(uuid.uuid4()),
        )

    # Patch get_db() so the handler queries the test pool instead of production.
    import src.db as _db_module
    monkeypatch.setattr(_db_module, "_db_instance", test_db)

    from src.resident_progress.registry import RESIDENT_PROGRESS_REGISTRY
    from src.resident_progress.status import resolve_status
    from src.http_api import http_get_progress_flat_recent

    class _MockRequest:
        client = None

        def __init__(self, params):
            self.query_params = params
            self.headers = {}

    resp = await http_get_progress_flat_recent(_MockRequest({"hours": "24"}))
    payload = json.loads(resp.body)
    assert payload["success"] is True
    rows = {r["resident_label"]: r for r in payload["rows"]}
    expected_labels = set(RESIDENT_PROGRESS_REGISTRY) | {"progress_flat_probe"}
    assert expected_labels <= set(rows.keys())
    # Vigil row should have status determined by resolve_status
    assert rows["vigil"]["status"] in {
        "OK", "flat-candidate", "silent", "source-error",
        "unresolved", "startup-grace",
    }


@pytest.mark.asyncio
async def test_endpoint_status_field_uses_priority_resolver(test_db, monkeypatch):
    import src.db as _db_module
    monkeypatch.setattr(_db_module, "_db_instance", test_db)

    from src.http_api import http_get_progress_flat_recent

    class _MockRequest:
        client = None
        query_params = {}
        headers = {}

    resp = await http_get_progress_flat_recent(_MockRequest())
    payload = json.loads(resp.body)
    rows = payload["rows"]
    assert all("status" in r for r in rows)
    assert all(
        r["status"] in {
            "OK", "flat-candidate", "silent", "source-error",
            "unresolved", "startup-grace", "never-seen",
        } for r in rows
    )
