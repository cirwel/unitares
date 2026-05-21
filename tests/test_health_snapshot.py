"""
Tests for src/services/health_snapshot.py and the cached-snapshot health_check
handler (Option F — ).
"""

import asyncio
import json
import time

import pytest
from unittest.mock import patch


# ============================================================================
# Cache module primitives
# ============================================================================

class TestHealthSnapshotCache:

    def setup_method(self):
        from src.services import health_snapshot
        health_snapshot.clear_snapshot()

    def teardown_method(self):
        from src.services import health_snapshot
        health_snapshot.clear_snapshot()

    def test_empty_cache_returns_nones(self):
        from src.services.health_snapshot import get_snapshot
        snapshot, age, produced = get_snapshot()
        assert snapshot is None
        assert age is None
        assert produced is None

    @pytest.mark.asyncio
    async def test_set_then_get_returns_snapshot(self):
        from src.services.health_snapshot import set_snapshot, get_snapshot
        data = {"status": "healthy", "checks": {"primary_db": {"status": "healthy"}}}
        await set_snapshot(data)
        snapshot, age, produced = get_snapshot()
        assert snapshot == data
        assert age is not None
        assert age >= 0
        assert age < 1.0  # Just set it, should be fresh
        assert produced is not None

    @pytest.mark.asyncio
    async def test_age_increases_over_time(self):
        from src.services.health_snapshot import set_snapshot, get_snapshot
        await set_snapshot({"status": "ok"})
        _, age1, _ = get_snapshot()
        await asyncio.sleep(0.05)
        _, age2, _ = get_snapshot()
        assert age2 > age1

    @pytest.mark.asyncio
    async def test_second_set_replaces_first(self):
        from src.services.health_snapshot import set_snapshot, get_snapshot
        await set_snapshot({"status": "first"})
        await set_snapshot({"status": "second"})
        snapshot, _, _ = get_snapshot()
        assert snapshot == {"status": "second"}

    def test_is_stale_with_none_age(self):
        from src.services.health_snapshot import is_stale
        assert is_stale(None) is True

    def test_is_stale_below_threshold(self):
        from src.services.health_snapshot import is_stale, STALENESS_THRESHOLD_SECONDS
        assert is_stale(STALENESS_THRESHOLD_SECONDS - 1) is False

    def test_is_stale_above_threshold(self):
        from src.services.health_snapshot import is_stale, STALENESS_THRESHOLD_SECONDS
        assert is_stale(STALENESS_THRESHOLD_SECONDS + 1) is True


# ============================================================================
# health_check handler reading from cache
# ============================================================================

def _parse_handler_response(result):
    """Handlers return [TextContent(...)] — extract the JSON payload."""
    assert len(result) >= 1
    text = result[0].text if hasattr(result[0], "text") else str(result[0])
    return json.loads(text)


class TestHealthCheckHandlerCached:

    def setup_method(self):
        from src.services import health_snapshot
        health_snapshot.clear_snapshot()

    def teardown_method(self):
        from src.services import health_snapshot
        health_snapshot.clear_snapshot()

    @pytest.mark.asyncio
    async def test_empty_cache_returns_error(self):
        from src.mcp_handlers.admin.handlers import handle_health_check
        result = await handle_health_check({})
        parsed = _parse_handler_response(result)
        # error_response returns {"success": False, "error": ...}
        assert parsed.get("success") is False or "error" in parsed
        text = json.dumps(parsed).lower()
        assert "not yet available" in text or "snapshot" in text

    @pytest.mark.asyncio
    async def test_populated_cache_returns_snapshot_lite(self):
        from src.services.health_snapshot import set_snapshot
        from src.mcp_handlers.admin.handlers import handle_health_check
        await set_snapshot({
            "status": "healthy",
            "version": "2.13.0",
            "redis_present": True,
            "identity_continuity_mode": "redis",
            "status_breakdown": {"healthy": 5, "warning": 0, "error": 0, "deprecated": 0, "unavailable": 0},
            "operator_summary": {"overall_status": "healthy", "failing_checks": [], "degraded_checks": [], "first_action": "No action needed.", "identity_continuity_mode": "redis"},
            "timestamp": "2026-04-10T00:00:00",
            "checks": {
                "primary_db": {"status": "healthy", "info": {"pool_size": 5}, "configured_backend": "postgres"},
                "redis_cache": {"status": "healthy", "present": True, "features": ["session_cache"]},
            },
        })

        result = await handle_health_check({"lite": True})
        parsed = _parse_handler_response(result)
        # success_response wraps in {"success": True, ...other fields at top level}
        assert parsed.get("status") == "healthy"
        assert parsed["checks"]["primary_db"]["status"] == "healthy"
        # Lite strips per-check detail keys like info/features
        assert "info" not in parsed["checks"]["primary_db"]
        assert "_cache" in parsed
        assert parsed["_cache"]["stale"] is False
        assert parsed["_cache"]["age_seconds"] is not None

    @pytest.mark.asyncio
    async def test_populated_cache_returns_full_snapshot(self):
        from src.services.health_snapshot import set_snapshot
        from src.mcp_handlers.admin.handlers import handle_health_check
        await set_snapshot({
            "status": "healthy",
            "version": "2.13.0",
            "redis_present": True,
            "checks": {
                "primary_db": {"status": "healthy", "info": {"pool_size": 5}, "configured_backend": "postgres"},
            },
        })
        result = await handle_health_check({"lite": False})
        parsed = _parse_handler_response(result)
        # Full mode preserves the info dict
        assert parsed["checks"]["primary_db"]["info"] == {"pool_size": 5}
        assert parsed["_cache"]["stale"] is False

    @pytest.mark.asyncio
    async def test_handler_does_not_call_get_health_check_data(self):
        """Regression: the handler must read from cache, never invoke the
        deadlock-prone get_health_check_data function directly."""
        from src.services.health_snapshot import set_snapshot
        from src.mcp_handlers.admin.handlers import handle_health_check

        await set_snapshot({"status": "healthy", "checks": {}})
        with patch("src.services.runtime_queries.get_health_check_data") as mock_gcd:
            await handle_health_check({})
            mock_gcd.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_flag_trips_when_age_exceeds_threshold(self):
        """Monkey-patch the monotonic baseline so age looks large without sleeping."""
        from src.services import health_snapshot
        from src.mcp_handlers.admin.handlers import handle_health_check

        await health_snapshot.set_snapshot({"status": "healthy", "checks": {}})
        # Rewind the recorded baseline so get_snapshot reports a stale age
        health_snapshot._snapshot_monotonic = time.monotonic() - (health_snapshot.STALENESS_THRESHOLD_SECONDS + 10)

        result = await handle_health_check({})
        parsed = _parse_handler_response(result)
        assert parsed["_cache"]["stale"] is True
        assert parsed["_cache"]["age_seconds"] >= health_snapshot.STALENESS_THRESHOLD_SECONDS
