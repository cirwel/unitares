"""Tests for src/fleet_metrics/{catalog,storage}.py."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.fleet_metrics.catalog import Metric, catalog as _catalog, register, require


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_initial_entry_present(self):
        """The module registers tokei.unitares.src.code on import."""
        assert "tokei.unitares.src.code" in _catalog
        entry = _catalog["tokei.unitares.src.code"]
        assert entry.unit == "lines"
        assert "unitares" in entry.description

    def test_db_backed_metrics_registered(self):
        """Chronicler's DB-backed scrapers have catalog entries so the server
        accepts POSTs from them — a missing catalog entry returns 404."""
        for name in ("agents.active.7d", "kg.entries.count", "checkins.7d"):
            assert name in _catalog, f"{name} missing from catalog"

    def test_governance_metrics_registered(self):
        """The governance-health series must be in the catalog or their daily
        POSTs 404 silently and the charts never populate."""
        for name in (
            "governance.coherence.mean.7d",
            "governance.risk.mean.7d",
            "governance.guide.7d",
            "governance.pause.7d",
            "governance.sentinel.findings.7d",
        ):
            assert name in _catalog, f"{name} missing from catalog"

    def test_every_scraper_has_a_catalog_entry(self):
        """Invariant: every name Chronicler scrapes must be catalog-registered.

        The POST endpoint validates against the catalog, so any SCRAPERS name
        without an entry is a silent 404 each daily run. This subset check
        guards all current and future scrapers at once, rather than relying on
        per-name lists drifting in step."""
        from agents.chronicler.scrapers import SCRAPERS

        missing = sorted(name for name in SCRAPERS if name not in _catalog)
        assert not missing, f"scrapers missing catalog entries (will 404): {missing}"

    def test_require_known_name_returns_entry(self):
        entry = require("tokei.unitares.src.code")
        assert isinstance(entry, Metric)

    def test_require_unknown_name_raises_keyerror(self):
        with pytest.raises(KeyError, match="not in the catalog"):
            require("nope.does.not.exist")

    def test_register_idempotent_on_identical(self):
        m = Metric(name="test.idempotent", description="x", unit="y")
        register(m)
        try:
            register(m)  # second call with same fields — must not raise
            assert _catalog["test.idempotent"] == m
        finally:
            _catalog.pop("test.idempotent", None)
            _catalog.pop("test.idempotent.error", None)

    def test_register_creates_error_twin(self):
        """Every registered metric gets a paired `.error` entry so Chronicler
        can post `<name>.error = 1` on scrape failure without 404ing under the
        catalog gate. Without this, scraper failures are silently swallowed."""
        m = Metric(name="test.twin", description="primary", unit="things")
        register(m)
        try:
            twin = _catalog.get("test.twin.error")
            assert twin is not None, "register() must auto-create .error twin"
            assert twin.name == "test.twin.error"
            assert twin.unit == "errors"
            assert "test.twin" in twin.description
        finally:
            _catalog.pop("test.twin", None)
            _catalog.pop("test.twin.error", None)

    def test_register_idempotent_includes_twin(self):
        """Re-registering a metric whose twin already exists must not raise."""
        m = Metric(name="test.twin.idem", description="x", unit="y")
        register(m)
        try:
            register(m)  # twin already exists; must not raise
            assert "test.twin.idem.error" in _catalog
        finally:
            _catalog.pop("test.twin.idem", None)
            _catalog.pop("test.twin.idem.error", None)

    def test_existing_scrapers_have_error_twins(self):
        """The shipping Chronicler scrapers must all have catalog-registered
        `.error` twins so their failure-visibility path actually lands rows
        in metrics.series instead of 404ing."""
        for base in (
            "tokei.unitares.src.code",
            "tests.unitares.count",
            "agents.active.7d",
            "kg.entries.count",
            "checkins.7d",
        ):
            assert f"{base}.error" in _catalog, f"missing .error twin for {base}"

    def test_register_does_not_create_double_error_twin(self):
        """Registering a metric whose name already ends in `.error` must not
        produce a `.error.error` entry — the auto-twin logic guards against
        compounding suffixes."""
        m = Metric(name="test.bare.error", description="standalone error", unit="errors")
        register(m)
        try:
            assert "test.bare.error" in _catalog
            assert "test.bare.error.error" not in _catalog
        finally:
            _catalog.pop("test.bare.error", None)

    def test_register_conflict_raises(self):
        m1 = Metric(name="test.conflict", description="a", unit="u1")
        m2 = Metric(name="test.conflict", description="b", unit="u2")
        register(m1)
        try:
            with pytest.raises(ValueError, match="different"):
                register(m2)
        finally:
            _catalog.pop("test.conflict", None)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _make_db_mock() -> tuple[MagicMock, AsyncMock]:
    """Return (db, conn) pair where `db.acquire()` yields the mocked conn."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)

    db = MagicMock()
    db.acquire = MagicMock(return_value=acquire_cm)
    return db, conn


class TestRecord:
    @pytest.mark.asyncio
    async def test_record_known_metric_no_ts_uses_db_default(self):
        from src.fleet_metrics import record

        db, conn = _make_db_mock()
        with patch("src.agent_storage.get_db", return_value=db):
            await record("tokei.unitares.src.code", 70000.0)

        conn.execute.assert_awaited_once()
        args = conn.execute.await_args.args
        assert "INSERT INTO metrics.series (name, value)" in args[0]
        assert args[1] == "tokei.unitares.src.code"
        assert args[2] == 70000.0

    @pytest.mark.asyncio
    async def test_record_with_explicit_ts(self):
        from src.fleet_metrics import record

        db, conn = _make_db_mock()
        ts = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
        with patch("src.agent_storage.get_db", return_value=db):
            await record("tokei.unitares.src.code", 1.5, ts=ts)

        args = conn.execute.await_args.args
        assert "INSERT INTO metrics.series (ts, name, value)" in args[0]
        assert args[1] == ts
        assert args[2] == "tokei.unitares.src.code"
        assert args[3] == 1.5

    @pytest.mark.asyncio
    async def test_record_unknown_metric_raises_without_db_hit(self):
        from src.fleet_metrics import record

        db, conn = _make_db_mock()
        with patch("src.agent_storage.get_db", return_value=db):
            with pytest.raises(KeyError):
                await record("unknown.metric", 1.0)
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_record_coerces_int_to_float(self):
        from src.fleet_metrics import record

        db, conn = _make_db_mock()
        with patch("src.agent_storage.get_db", return_value=db):
            await record("tokei.unitares.src.code", 42)
        args = conn.execute.await_args.args
        assert isinstance(args[2], float)
        assert args[2] == 42.0


class TestQuery:
    @pytest.mark.asyncio
    async def test_query_returns_parsed_points(self):
        from src.fleet_metrics import query

        db, conn = _make_db_mock()
        ts1 = datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        conn.fetch.return_value = [
            {"ts": ts1, "value": 100.0},
            {"ts": ts2, "value": 101.5},
        ]
        with patch("src.agent_storage.get_db", return_value=db):
            points = await query("tokei.unitares.src.code")
        assert len(points) == 2
        assert points[0].ts == ts1
        assert points[0].value == 100.0
        assert points[1].value == 101.5

    @pytest.mark.asyncio
    async def test_query_with_time_range_uses_bounded_sql(self):
        from src.fleet_metrics import query

        db, conn = _make_db_mock()
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)
        until = datetime(2026, 4, 20, tzinfo=timezone.utc)
        with patch("src.agent_storage.get_db", return_value=db):
            await query("x", since=since, until=until, limit=50)
        args = conn.fetch.await_args.args
        assert "ts >= $2 AND ts <= $3" in args[0]
        assert args[1] == "x"
        assert args[2] == since
        assert args[3] == until
        assert args[4] == 50

    @pytest.mark.asyncio
    async def test_query_caps_limit_at_10k(self):
        from src.fleet_metrics import query

        db, conn = _make_db_mock()
        with patch("src.agent_storage.get_db", return_value=db):
            await query("x", limit=99999)
        args = conn.fetch.await_args.args
        assert args[-1] == 10_000

    @pytest.mark.asyncio
    async def test_query_zero_or_negative_limit_short_circuits(self):
        from src.fleet_metrics import query

        db, conn = _make_db_mock()
        with patch("src.agent_storage.get_db", return_value=db):
            result = await query("x", limit=0)
        assert result == []
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_query_unknown_name_does_not_raise(self):
        """Reads don't enforce catalog membership (forensic use case)."""
        from src.fleet_metrics import query

        db, conn = _make_db_mock()
        with patch("src.agent_storage.get_db", return_value=db):
            result = await query("no.such.metric")
        assert result == []
