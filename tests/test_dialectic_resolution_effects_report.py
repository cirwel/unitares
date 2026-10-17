"""Safety and rendering tests for the resolution-effect operator inventory."""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys
import types
from unittest.mock import MagicMock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dialectic_resolution_effects",
    REPO_ROOT / "scripts" / "ops" / "dialectic_resolution_effects.py",
)
report = importlib.util.module_from_spec(SPEC)
sys.modules["dialectic_resolution_effects"] = report
SPEC.loader.exec_module(report)


def _row(**overrides):
    row = {
        "session_id": "session-1",
        "state": "applying",
        "attempt_count": 2,
        "owner_token": "owner-exact-123",
        "last_error": "worker response lost",
        "effect_required": True,
        "pause_token": "pause-1",
        "state_age_seconds": 3_700,
        "session_status": "resolved",
        "session_phase": "resolved",
        "awaiting_facilitation": False,
        "agent_status": "paused",
        "identity_status": "paused",
        "current_pause_token": "pause-1",
        "updated_at": dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc),
    }
    row.update(overrides)
    return row


def test_query_selects_only_actionable_states_and_live_pause_evidence():
    assert "e.state = ANY" in report.QUERY
    assert "core.dialectic_sessions" in report.QUERY
    assert "core.agents" in report.QUERY
    assert "core.identities" in report.QUERY
    assert "current_pause_token" in report.QUERY
    assert report.ACTIONABLE_STATES == (
        "pending", "applying", "unknown_partial"
    )


def test_database_connection_is_server_enforced_read_only(monkeypatch):
    conn = MagicMock()
    cursor = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    cursor.fetchall.return_value = []

    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.connect = MagicMock(return_value=conn)
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object()
    psycopg2.extras = extras
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)

    assert report._run_read_only("dsn", "SELECT 1", {}) == []
    conn.set_session.assert_called_once_with(readonly=True)
    assert cursor.execute.call_count == 2


def test_default_dsn_uses_server_configuration_first(monkeypatch):
    monkeypatch.setenv("DB_POSTGRES_URL", "postgresql://server")
    monkeypatch.setenv("GOVERNANCE_DATABASE_URL", "postgresql://legacy")

    assert report.default_dsn() == "postgresql://server"


def test_default_dsn_preserves_legacy_and_local_fallbacks(monkeypatch):
    monkeypatch.delenv("DB_POSTGRES_URL", raising=False)
    monkeypatch.setenv("GOVERNANCE_DATABASE_URL", "postgresql://legacy")
    assert report.default_dsn() == "postgresql://legacy"

    monkeypatch.delenv("GOVERNANCE_DATABASE_URL", raising=False)
    assert report.default_dsn() == report.FALLBACK_DSN


def test_explicit_dsn_overrides_environment(monkeypatch, capsys):
    captured = {}

    def fake_fetch(dsn, _states):
        captured["dsn"] = dsn
        return []

    monkeypatch.setenv("DB_POSTGRES_URL", "postgresql://server")
    monkeypatch.setattr(report, "fetch", fake_fetch)

    assert report.main(["--dsn", "postgresql://explicit"]) == 0
    assert captured["dsn"] == "postgresql://explicit"
    assert "No non-applied" in capsys.readouterr().out


def test_render_exposes_exact_fence_token_and_live_state():
    rendered = report.render([_row()])
    assert "owner_token=owner-exact-123" in rendered
    assert "[applying, age=1h, attempts=2]" in rendered
    assert "session=resolved/resolved" in rendered
    assert "generation:matches_effect" in rendered
    assert "last_error=worker response lost" in rendered
    assert "Do not replay applying or unknown_partial" in rendered


def test_pause_generation_state_does_not_infer_effect_completion():
    assert report.pause_generation_state(_row()) == "matches_effect"
    assert report.pause_generation_state(
        _row(current_pause_token="pause-2")
    ) == "pause_token_mismatch"
    assert report.pause_generation_state(
        _row(agent_status="active", identity_status="active")
    ) == "not_paused"
    assert report.pause_generation_state(
        _row(agent_status="paused", identity_status="active")
    ) == "status_mismatch"
    assert report.pause_generation_state(
        _row(effect_required=False, pause_token=None)
    ) == "not_required"


def test_json_output_serializes_timestamps_and_honors_state_filter(
    monkeypatch, capsys,
):
    captured = {}

    def fake_fetch(_dsn, states):
        captured["states"] = states
        return [_row(state="pending")]

    monkeypatch.setattr(report, "fetch", fake_fetch)
    assert report.main(["--json", "--state", "pending"]) == 0
    output = capsys.readouterr().out
    assert '"updated_at": "2026-10-16T00:00:00+00:00"' in output
    assert captured["states"] == ("pending",)


def test_empty_inventory_is_explicit():
    assert report.render([]) == "No non-applied dialectic resolution effects."
