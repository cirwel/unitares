"""Tests for the enrichment pipeline registry and runner."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# Import enrichments to trigger registration
import src.mcp_handlers.updates.enrichments  # noqa: F401

from src.mcp_handlers.updates.pipeline import (
    get_enrichment_count,
    get_enrichment_names,
    run_enrichment_pipeline,
    _ENRICHMENTS,
)


class TestEnrichmentRegistration:
    def test_all_enrichments_registered(self):
        assert get_enrichment_count() == 30

    def test_enrichment_order_is_unique(self):
        orders = [e.order for e in _ENRICHMENTS]
        assert len(orders) == len(set(orders)), f"Duplicate orders: {orders}"

    def test_ordering_constraints(self):
        names = get_enrichment_names()
        idx = {n: i for i, n in enumerate(names)}
        # state_interpretation before actionable_feedback before llm_coaching
        assert idx["enrich_state_interpretation"] < idx["enrich_actionable_feedback"]
        assert idx["enrich_actionable_feedback"] < idx["enrich_llm_coaching"]
        assert idx["enrich_health_status_toplevel"] < idx["enrich_input_glossary"]
        assert idx["enrich_input_glossary"] < idx["enrich_cirs_response_fields"]
        # mirror_signals runs late
        assert idx["enrich_mirror_signals"] > idx["enrich_websocket_broadcast"]

    def test_enrichments_sorted_by_order(self):
        orders = [e.order for e in _ENRICHMENTS]
        assert orders == sorted(orders)


class TestPipelineRunner:
    @pytest.mark.asyncio
    async def test_pipeline_runner_isolates_failures(self):
        """A failing enrichment must not prevent subsequent ones from running."""
        from src.mcp_handlers.updates.pipeline import (
            _EnrichmentEntry,
            _ENRICHMENTS,
        )

        call_log = []

        def good_before(ctx):
            call_log.append("before")

        def bad(ctx):
            call_log.append("bad")
            raise RuntimeError("boom")

        def good_after(ctx):
            call_log.append("after")

        # Temporarily replace the registry
        original = list(_ENRICHMENTS)
        _ENRICHMENTS.clear()
        _ENRICHMENTS.extend([
            _EnrichmentEntry(fn=good_before, order=1, name="good_before", is_async=False),
            _EnrichmentEntry(fn=bad, order=2, name="bad", is_async=False),
            _EnrichmentEntry(fn=good_after, order=3, name="good_after", is_async=False),
        ])

        try:
            ctx = MagicMock()
            await run_enrichment_pipeline(ctx)
            assert call_log == ["before", "bad", "after"]
        finally:
            _ENRICHMENTS.clear()
            _ENRICHMENTS.extend(original)

    @pytest.mark.asyncio
    async def test_pipeline_runner_handles_async(self):
        from src.mcp_handlers.updates.pipeline import (
            _EnrichmentEntry,
            _ENRICHMENTS,
        )

        call_log = []

        async def async_fn(ctx):
            call_log.append("async")

        def sync_fn(ctx):
            call_log.append("sync")

        original = list(_ENRICHMENTS)
        _ENRICHMENTS.clear()
        _ENRICHMENTS.extend([
            _EnrichmentEntry(fn=sync_fn, order=1, name="sync_fn", is_async=False),
            _EnrichmentEntry(fn=async_fn, order=2, name="async_fn", is_async=True),
        ])

        try:
            ctx = MagicMock()
            await run_enrichment_pipeline(ctx)
            assert call_log == ["sync", "async"]
        finally:
            _ENRICHMENTS.clear()
            _ENRICHMENTS.extend(original)


class TestLiteModeSkipping:
    """response_mode='minimal' callers (e.g. anima-broker) skip lite_safe enrichments.

    The broker reads action+margin from the locked-update phase and discards the
    rest of the response. Running expensive response-shaping enrichments for it
    only inflates p95 latency past the broker's 5s client timeout. lite_safe=True
    on an enrichment marks it as response-shaping-only — safe to skip in lite
    mode, runs normally otherwise.
    """

    @pytest.mark.asyncio
    async def test_lite_safe_runs_when_response_mode_is_full(self):
        from src.mcp_handlers.updates.pipeline import (
            _EnrichmentEntry,
            _ENRICHMENTS,
        )

        call_log = []

        def heavy(ctx):
            call_log.append("heavy")

        def cheap(ctx):
            call_log.append("cheap")

        original = list(_ENRICHMENTS)
        _ENRICHMENTS.clear()
        _ENRICHMENTS.extend([
            _EnrichmentEntry(fn=cheap, order=1, name="cheap", is_async=False, lite_safe=False),
            _EnrichmentEntry(fn=heavy, order=2, name="heavy", is_async=False, lite_safe=True),
        ])

        try:
            ctx = MagicMock()
            ctx.arguments = {"response_mode": "full"}
            await run_enrichment_pipeline(ctx)
            assert call_log == ["cheap", "heavy"]
        finally:
            _ENRICHMENTS.clear()
            _ENRICHMENTS.extend(original)

    @pytest.mark.asyncio
    async def test_lite_safe_skipped_when_response_mode_is_minimal(self):
        from src.mcp_handlers.updates.pipeline import (
            _EnrichmentEntry,
            _ENRICHMENTS,
        )

        call_log = []

        def heavy(ctx):
            call_log.append("heavy")

        def cheap(ctx):
            call_log.append("cheap")

        original = list(_ENRICHMENTS)
        _ENRICHMENTS.clear()
        _ENRICHMENTS.extend([
            _EnrichmentEntry(fn=cheap, order=1, name="cheap", is_async=False, lite_safe=False),
            _EnrichmentEntry(fn=heavy, order=2, name="heavy", is_async=False, lite_safe=True),
        ])

        try:
            ctx = MagicMock()
            ctx.arguments = {"response_mode": "minimal"}
            await run_enrichment_pipeline(ctx)
            assert call_log == ["cheap"]
        finally:
            _ENRICHMENTS.clear()
            _ENRICHMENTS.extend(original)

    @pytest.mark.asyncio
    async def test_lite_alias_runs_lite_safe_enrichments(self):
        """response_mode='lite' is compact, not the legacy skinny path."""
        from src.mcp_handlers.updates.pipeline import (
            _EnrichmentEntry,
            _ENRICHMENTS,
        )

        call_log = []

        def heavy(ctx):
            call_log.append("heavy")

        def cheap(ctx):
            call_log.append("cheap")

        original = list(_ENRICHMENTS)
        _ENRICHMENTS.clear()
        _ENRICHMENTS.extend([
            _EnrichmentEntry(fn=cheap, order=1, name="cheap", is_async=False, lite_safe=False),
            _EnrichmentEntry(fn=heavy, order=2, name="heavy", is_async=False, lite_safe=True),
        ])

        try:
            ctx = MagicMock()
            ctx.arguments = {"response_mode": "lite"}
            await run_enrichment_pipeline(ctx)
            assert call_log == ["cheap", "heavy"]
        finally:
            _ENRICHMENTS.clear()
            _ENRICHMENTS.extend(original)

    @pytest.mark.asyncio
    async def test_lite_safe_skipped_when_env_default_is_minimal(self):
        """The skinny path also applies when minimal is selected by default."""
        from src.mcp_handlers.updates.pipeline import (
            _EnrichmentEntry,
            _ENRICHMENTS,
        )

        call_log = []

        def heavy(ctx):
            call_log.append("heavy")

        def cheap(ctx):
            call_log.append("cheap")

        original = list(_ENRICHMENTS)
        _ENRICHMENTS.clear()
        _ENRICHMENTS.extend([
            _EnrichmentEntry(fn=cheap, order=1, name="cheap", is_async=False, lite_safe=False),
            _EnrichmentEntry(fn=heavy, order=2, name="heavy", is_async=False, lite_safe=True),
        ])

        try:
            ctx = MagicMock()
            ctx.arguments = {}
            with patch.dict("os.environ", {"UNITARES_PROCESS_UPDATE_RESPONSE_MODE": "minimal"}):
                await run_enrichment_pipeline(ctx)
            assert call_log == ["cheap"]
        finally:
            _ENRICHMENTS.clear()
            _ENRICHMENTS.extend(original)

    @pytest.mark.asyncio
    async def test_lite_safe_skipped_when_agent_prefers_minimal(self):
        """Agent verbosity preferences use the same canonical response contract."""
        from src.mcp_handlers.updates.pipeline import (
            _EnrichmentEntry,
            _ENRICHMENTS,
        )

        call_log = []

        def heavy(ctx):
            call_log.append("heavy")

        def cheap(ctx):
            call_log.append("cheap")

        original = list(_ENRICHMENTS)
        _ENRICHMENTS.clear()
        _ENRICHMENTS.extend([
            _EnrichmentEntry(fn=cheap, order=1, name="cheap", is_async=False, lite_safe=False),
            _EnrichmentEntry(fn=heavy, order=2, name="heavy", is_async=False, lite_safe=True),
        ])

        try:
            ctx = MagicMock()
            ctx.arguments = {}
            ctx.meta = MagicMock()
            ctx.meta.preferences = {"verbosity": "minimal"}
            await run_enrichment_pipeline(ctx)
            assert call_log == ["cheap"]
        finally:
            _ENRICHMENTS.clear()
            _ENRICHMENTS.extend(original)

    @pytest.mark.asyncio
    async def test_non_lite_safe_runs_in_minimal_mode(self):
        """Side-effect enrichments (websocket_broadcast, basin_tracking, etc.)
        default lite_safe=False and must still run in minimal mode."""
        from src.mcp_handlers.updates.pipeline import (
            _EnrichmentEntry,
            _ENRICHMENTS,
        )

        call_log = []

        def side_effect(ctx):
            call_log.append("side_effect")

        original = list(_ENRICHMENTS)
        _ENRICHMENTS.clear()
        _ENRICHMENTS.extend([
            _EnrichmentEntry(fn=side_effect, order=1, name="side_effect", is_async=False, lite_safe=False),
        ])

        try:
            ctx = MagicMock()
            ctx.arguments = {"response_mode": "minimal"}
            await run_enrichment_pipeline(ctx)
            assert call_log == ["side_effect"]
        finally:
            _ENRICHMENTS.clear()
            _ENRICHMENTS.extend(original)

    def test_marked_enrichments_are_lite_safe(self):
        """Lock in which production enrichments are marked lite_safe so we don't
        silently flip a non-lite_safe one to skip during refactors."""
        names_to_lite_safe = {e.name: e.lite_safe for e in _ENRICHMENTS}
        # Currently marked lite_safe — these are the dominant cost
        # contributors per the [enrichment_phases] log analysis on 2026-05-04
        # (learning_context 4-9s every call, knowledge_surfacing 2.5s tail,
        # mirror_signals only meaningful for response_mode='mirror').
        assert names_to_lite_safe.get("enrich_learning_context") is True
        assert names_to_lite_safe.get("enrich_knowledge_surfacing") is True
        assert names_to_lite_safe.get("enrich_mirror_signals") is True
        assert names_to_lite_safe.get("enrich_input_glossary") is True
        # Side-effect enrichments must NOT be lite_safe — confirms intent.
        assert names_to_lite_safe.get("enrich_websocket_broadcast") is False
        assert names_to_lite_safe.get("enrich_basin_tracking") is False
        assert names_to_lite_safe.get("enrich_identity_notifications") is False
