"""Unit tests for scripts/ops/wave3_shadow_divergence_check.py (PR #597
review) — the alias→column derivation and row-kind logic, plus a drift
guard pinning the comparator's alias contract.

The runner derives the payload's `divergent_columns` by stripping `_diff`
from the comparator's SQL aliases. The original §8.2 sketch used abbreviated
aliases (provisional_diff, allow_rebind_diff, ...) which produced names
matching no real column — caught in council review. The contract is now:
every diff alias is exactly `<canonical_column>_diff`.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

_RUNNER_PATH = project_root / "scripts" / "ops" / "wave3_shadow_divergence_check.py"
_SQL_PATH = project_root / "scripts" / "ops" / "wave-3-shadow-divergence-check.sql"

spec = importlib.util.spec_from_file_location("wave3_shadow_divergence_check", _RUNNER_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

# Canonical column sets, pinned from the live schema 2026-06-10 (the
# migration-parity tests in test_migration_043_044_shadow_tables.py keep the
# shadow tables aligned with canonical; this pins the comparator's aliases
# against the same shape).
IDENTITIES_COLUMNS = {
    "identity_id", "agent_id", "api_key_hash", "created_at", "updated_at",
    "disabled_at", "parent_agent_id", "spawn_reason", "status", "metadata",
    "metadata_tsv", "last_activity_at", "provisional_lineage",
    "provisional_score_id", "provisional_recorded_at", "confirmed_at",
    "lineage_declared_at", "lineage_demoted_at", "lineage_archived_at",
    "lineage_last_eval_at", "chain_obs_count",
}
AGENTS_COLUMNS = {
    "id", "api_key", "status", "purpose", "notes", "tags", "created_at",
    "updated_at", "archived_at", "parent_agent_id", "spawn_reason", "label",
    "thread_id", "thread_position", "allow_rebind_after_exit",
    "allow_concurrent_contexts",
}


def _aliases_per_statement() -> list[set[str]]:
    """Extract `AS <name>_diff` aliases per comparator statement."""
    sql = _SQL_PATH.read_text()
    blocks = sql.split("-- core.agents divergence")
    assert len(blocks) == 2, "comparator file structure changed"
    return [
        set(re.findall(r"AS\s+([a-z0-9_]+)_diff\b", block)) for block in blocks
    ]


def test_every_sql_alias_stem_is_a_canonical_column():
    """Drift guard for the alias contract: stripping `_diff` from every
    comparator alias must yield a real canonical column name."""
    ident_aliases, agent_aliases = _aliases_per_statement()
    assert ident_aliases, "no identities aliases found — regex or file drift"
    assert agent_aliases, "no agents aliases found — regex or file drift"
    bad_ident = ident_aliases - IDENTITIES_COLUMNS
    bad_agent = agent_aliases - AGENTS_COLUMNS
    assert not bad_ident, f"identities aliases not matching canonical columns: {bad_ident}"
    assert not bad_agent, f"agents aliases not matching canonical columns: {bad_agent}"


def test_row_kind_three_kinds():
    assert runner._row_kind({"canonical_missing": True, "shadow_missing": False}) == "canonical_missing"
    assert runner._row_kind({"canonical_missing": False, "shadow_missing": True}) == "shadow_missing"
    assert runner._row_kind({"canonical_missing": False, "shadow_missing": False}) == "column_mismatch"


def test_divergent_columns_returns_exact_canonical_names():
    row = {
        "table_name": "identities",
        "agent_id": "ag-1",
        "canonical_missing": False,
        "shadow_missing": False,
        "provisional_lineage_diff": True,
        "lineage_declared_at_diff": True,
        "status_diff": False,
        "metadata_diff": None,  # NULL boolean from SQL — must not count
    }
    cols = runner._divergent_columns(row)
    assert cols == ["lineage_declared_at", "provisional_lineage"]
    for c in cols:
        assert c in IDENTITIES_COLUMNS


def test_divergent_columns_excludes_non_diff_keys_and_handles_agents():
    row = {
        "table_name": "agents",
        "agent_id": "ag-2",
        "canonical_missing": False,
        "shadow_missing": False,
        "allow_rebind_after_exit_diff": True,
        "allow_concurrent_contexts_diff": True,
        "tags_diff": True,
    }
    cols = runner._divergent_columns(row)
    assert cols == ["allow_concurrent_contexts", "allow_rebind_after_exit", "tags"]
    for c in cols:
        assert c in AGENTS_COLUMNS


def test_missing_row_has_empty_divergent_columns():
    """A shadow_missing row carries NULL diff booleans (full outer join) —
    the derived column list must be empty, matching the payload helper's
    kind/columns coherence rule."""
    row = {
        "table_name": "identities",
        "agent_id": "ag-3",
        "canonical_missing": False,
        "shadow_missing": True,
        "status_diff": None,
        "metadata_diff": None,
    }
    assert runner._divergent_columns(row) == []
    assert runner._row_kind(row) == "shadow_missing"


# --- tri-state shadow-window status -------------------------------------
# The original gate was a bare `count == 0` skip, which rendered two very
# different situations identically: a window that never started (correct
# today -- no BEAM shadow writer exists) and a window that started and then
# stopped (a broken writer). The second read as "nothing to do", which is the
# failure mode the checker exists to catch.


def test_window_status_active_when_rows_present():
    assert runner._window_status("identities", 5, {}) == "active"
    # A prior marker does not change an active read.
    assert (
        runner._window_status("identities", 5, {"identities": {"ever_active": True}})
        == "active"
    )


def test_window_status_never_started_when_empty_and_no_marker():
    assert runner._window_status("identities", 0, {}) == "never_started"
    assert runner._window_status("agents", 0, {"agents": {}}) == "never_started"


def test_window_status_went_dark_when_empty_after_active():
    """The case the bare count==0 gate could not express."""
    state = {"identities": {"ever_active": True, "last_count": 42}}
    assert runner._window_status("identities", 0, state) == "went_dark"


def test_window_status_is_per_table_not_global():
    """One table going dark must not mask the other's real state."""
    state = {"identities": {"ever_active": True}}
    assert runner._window_status("identities", 0, state) == "went_dark"
    assert runner._window_status("agents", 0, state) == "never_started"


def test_corrupt_marker_degrades_to_never_started_not_crash(tmp_path, monkeypatch):
    """A corrupt marker must not take the checker down, but it is a blind
    spot and the runner says so on stderr rather than failing silently."""
    bad = tmp_path / "window_state.json"
    bad.write_text("{not json")
    monkeypatch.setattr(runner, "WINDOW_STATE_FILE", bad)
    assert runner._load_window_state() == {}


def test_missing_marker_reads_as_empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WINDOW_STATE_FILE", tmp_path / "absent.json")
    assert runner._load_window_state() == {}


def test_window_state_round_trips(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "window_state.json"
    monkeypatch.setattr(runner, "WINDOW_STATE_FILE", path)
    runner._save_window_state({"identities": {"ever_active": True, "last_count": 3}})
    assert runner._load_window_state()["identities"]["ever_active"] is True


def test_self_test_mode_exists():
    """The comparator must be able to prove it can see; a zero from a normal
    run is only evidence once this has run green."""
    assert callable(runner.self_test)
