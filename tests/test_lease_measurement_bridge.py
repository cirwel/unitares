"""Tests for the §14 prereq PR #6 lease measurement bridge.

The lease client records one sample per RPC into a bounded in-process deque
(src/lease_plane/client.py); the MCP server's perf_monitor_persist_task
drains the deque in batches into audit.coordination_measurements as
measurement.lease_plane.request rows — the disconfirmer-(B) Phase A
baseline. These tests pin: sample capture at all three recorder call sites,
drain semantics (pop-all, bounded-drop counting), and the INSERT row shape
against the real migration-041 table in governance_test.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.lease_plane import client as lease_client
from src.lease_plane.client import (
    _record_lease_rpc_latency,
    drain_measurement_samples,
    measurement_samples_dropped,
)


@pytest.fixture(autouse=True)
def _clean_sample_buffer():
    drain_measurement_samples()
    yield
    drain_measurement_samples()


class TestSampleCaptureAndDrain:
    def test_recorder_appends_sample_with_full_shape(self):
        start = time.perf_counter()
        _record_lease_rpc_latency(
            "/v1/lease/acquire", start, "ok", method="POST", payload_bytes=512
        )
        samples = drain_measurement_samples()
        assert len(samples) == 1
        ts, endpoint, method, outcome, elapsed_ms, payload_bytes = samples[0]
        assert isinstance(ts, datetime) and ts.tzinfo is not None
        assert endpoint == "/v1/lease/acquire"
        assert method == "POST"
        assert outcome == "ok"
        assert isinstance(elapsed_ms, int) and elapsed_ms >= 0
        assert payload_bytes == 512

    def test_drain_empties_the_buffer(self):
        start = time.perf_counter()
        for _ in range(5):
            _record_lease_rpc_latency("/v1/lease/renew", start, "ok", method="POST")
        assert len(drain_measurement_samples()) == 5
        assert drain_measurement_samples() == []

    def test_error_outcomes_are_sampled_too(self):
        start = time.perf_counter()
        _record_lease_rpc_latency(
            "/v1/lease/acquire", start, "transport_exception", method="POST"
        )
        _record_lease_rpc_latency(
            "/v1/lease/status", start, "schema_invalid", method="GET"
        )
        outcomes = {s[3] for s in drain_measurement_samples()}
        assert outcomes == {"transport_exception", "schema_invalid"}

    def test_maxlen_bound_drops_oldest_and_counts(self):
        start = time.perf_counter()
        before_dropped = measurement_samples_dropped()
        maxlen = lease_client._MEASUREMENT_SAMPLES.maxlen
        for i in range(maxlen + 3):
            _record_lease_rpc_latency(f"/v1/lease/n{i}", start, "ok", method="POST")
        samples = drain_measurement_samples()
        assert len(samples) == maxlen
        # Oldest dropped: the first surviving sample is n3, not n0.
        assert samples[0][1] == "/v1/lease/n3"
        assert measurement_samples_dropped() == before_dropped + 3

    def test_request_json_records_through_the_extended_recorder(self):
        """End-to-end through _request_json with a stub transport: the
        sample carries the request path, method, outcome, and body bytes."""
        from src.lease_plane import PROTOCOL_VERSION
        from src.lease_plane.client import LeasePlaneClient

        captured_body = {"surface_id": "file:///tmp/x", "ttl_s": 300}

        def stub_transport(request):
            return {"ok": True, "protocol_version": PROTOCOL_VERSION}

        c = LeasePlaneClient(transport=stub_transport)
        c._request_json("POST", "/v1/lease/acquire", captured_body)
        samples = drain_measurement_samples()
        assert len(samples) == 1
        _, endpoint, method, outcome, _, payload_bytes = samples[0]
        assert endpoint == "/v1/lease/acquire"
        assert method == "POST"
        assert outcome == "ok"
        expected_bytes = len(
            json.dumps(captured_body, separators=(",", ":")).encode("utf-8")
        )
        assert payload_bytes == expected_bytes


# --- Integration: row shape against the real migration-041 table -----------

try:
    import asyncpg
except ImportError:
    asyncpg = None

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

needs_db = pytest.mark.skipif(
    asyncpg is None or not can_connect_to_test_db(),
    reason="governance_test database not available",
)


@needs_db
@pytest.mark.asyncio
async def test_drained_samples_insert_into_coordination_measurements():
    """The exact INSERT the persist task issues must satisfy migration 041's
    CHECKs (measurement_type namespace, meta jsonb object, NOT NULLs)."""
    from governance_core.coordination_events_helpers import make_measurement_payload
    from src.coordination_events import MEASUREMENT_LEASE_PLANE_REQUEST

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    marker_endpoint = f"/v1/lease/test-{int(time.time())}"
    try:
        sample = (datetime.now(UTC), marker_endpoint, "POST", "ok", 28, 512)
        ts, endpoint, method, outcome, elapsed_ms, payload_bytes = sample
        meta = make_measurement_payload(
            endpoint=endpoint,
            method=method,
            status_code=None,
            elapsed_ms=elapsed_ms,
            payload_bytes=payload_bytes,
        ) | {"samples_dropped_total": 0}
        await conn.execute(
            """
            INSERT INTO audit.coordination_measurements
                (recorded_at, measurement_type, endpoint,
                 elapsed_ms, status, payload_bytes, meta)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            ts,
            MEASUREMENT_LEASE_PLANE_REQUEST,
            endpoint,
            elapsed_ms,
            outcome,
            payload_bytes,
            json.dumps(meta),
        )
        row = await conn.fetchrow(
            """
            SELECT measurement_type, endpoint, elapsed_ms, status,
                   payload_bytes, meta
            FROM audit.coordination_measurements
            WHERE endpoint = $1
            """,
            marker_endpoint,
        )
        assert row["measurement_type"] == "measurement.lease_plane.request"
        assert row["elapsed_ms"] == 28
        assert row["status"] == "ok"
        assert row["payload_bytes"] == 512
        meta_back = json.loads(row["meta"])
        assert meta_back["method"] == "POST"
        assert meta_back["samples_dropped_total"] == 0
    finally:
        await conn.execute(
            "DELETE FROM audit.coordination_measurements WHERE endpoint = $1",
            marker_endpoint,
        )
        await conn.close()
