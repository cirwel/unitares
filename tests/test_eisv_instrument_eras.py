"""EISV instrument eras and the per-row instrument stamp.

Covers the registry contract (config/eisv_instrument_eras.json), the
analysis-side window guard (src/eisv_eras.py), the row stamp and its
per-monitor check-in count (src/eisv_instrument.py), and the wiring that
persists both.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import eisv_eras, eisv_instrument
from tests import eisv_preservation_driver as driver

REPO_ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_caches():
    eisv_instrument.reset_caches()
    yield
    eisv_instrument.reset_caches()


def _write_registry(tmp_path: Path, eras: list[dict]) -> Path:
    path = tmp_path / "eras.json"
    path.write_text(json.dumps({"schema": eisv_eras.REGISTRY_SCHEMA, "eras": eras}), encoding="utf-8")
    return path


def _entry(id_, at, kind="value", **extra):
    return {"id": id_, "kind": kind, "effective_at": at, "summary": id_, **extra}


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------


def test_registry_entries_are_well_formed_and_ordered():
    raw = json.loads(eisv_eras.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert raw["schema"] == eisv_eras.REGISTRY_SCHEMA
    ids = [e["id"] for e in raw["eras"]]
    assert len(ids) == len(set(ids))
    for entry in raw["eras"]:
        assert entry["kind"] in eisv_eras.ENTRY_KINDS, entry["id"]
        assert entry.get("summary"), entry["id"]
        assert entry.get("refs"), entry["id"]
        if entry.get("effective_at") is not None:
            assert entry.get("effective_at_basis"), entry["id"]

    boundaries = eisv_eras.load_boundaries()
    timed = [b for b in boundaries if b.effective_at is not None]
    assert [b.effective_at for b in timed] == sorted(b.effective_at for b in timed)
    for b in timed:
        lo, hi = b.interval
        assert lo <= b.effective_at <= hi, b.id


def test_untimed_entries_only_after_every_timed_entry():
    """An entry without a time is a stamp-only boundary, so it must be newest."""
    boundaries = eisv_eras.load_boundaries()
    seen_untimed = False
    for b in boundaries:
        if b.effective_at is None:
            seen_untimed = True
        else:
            assert not seen_untimed, f"{b.id} is timed but follows an untimed entry"


def test_a_changed_golden_file_requires_a_new_era_entry():
    """Every change the golden trajectories pin must append a registry entry.

    The newest entry that records a golden hash must record the current one.
    A maths PR regenerates the golden file, so it has to add an entry with the
    new hash, and the stamp on every later row then names that era.
    """
    digest = hashlib.sha256(driver.GOLDEN_PATH.read_bytes()).hexdigest()
    pinned = [e for e in eisv_eras.load_registry()["eras"] if e.get("golden_sha256")]
    assert pinned, "no era entry records the golden trajectory hash"
    newest = pinned[-1]
    assert newest["golden_sha256"] == digest, (
        f"The golden trajectory file changed (sha256 {digest}) but the newest era "
        f"entry that records one is {newest['id']} ({newest['golden_sha256']}). Append "
        "an entry to config/eisv_instrument_eras.json for this instrument change, with "
        "golden_sha256 set to the new hash; effective_at may stay null until deployed."
    )
    assert newest["kind"] in eisv_eras.SPLITTING_KINDS
    assert eisv_eras.current_era_id() == newest["id"] or any(
        e["kind"] in eisv_eras.SPLITTING_KINDS
        for e in eisv_eras.load_registry()["eras"][
            eisv_eras.load_registry()["eras"].index(newest) + 1:
        ]
    )


def test_the_golden_guard_fires_on_an_unrecorded_change(monkeypatch, tmp_path):
    changed = tmp_path / "golden.json"
    changed.write_bytes(driver.GOLDEN_PATH.read_bytes() + b"\n")
    monkeypatch.setattr(driver, "GOLDEN_PATH", changed)
    with pytest.raises(AssertionError, match="Append an entry"):
        test_a_changed_golden_file_requires_a_new_era_entry()


def test_current_era_is_the_newest_splitting_entry(tmp_path):
    path = _write_registry(tmp_path, [
        _entry("X1", "2026-01-01T00:00:00Z"),
        _entry("X2", "2026-02-01T00:00:00Z"),
        _entry("L", "2026-03-01T00:00:00Z", kind="label_only"),
    ])
    assert eisv_eras.current_era_id(path) == "X2"


# ---------------------------------------------------------------------------
# Window guard
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path):
    return _write_registry(tmp_path, [
        _entry("X1", "2026-01-10T00:00:00Z"),
        _entry("L", "2026-01-15T00:00:00Z", kind="label_only"),
        _entry("X2", "2026-01-20T12:00:00Z", ambiguous_from="2026-01-20T11:00:00Z",
               ambiguous_until="2026-01-20T13:00:00Z"),
        _entry("P", "2026-01-25T00:00:00Z", kind="population"),
    ])


def test_window_inside_one_era_crosses_nothing(registry):
    assert eisv_eras.boundaries_crossed(_t("2026-01-11"), _t("2026-01-19"), path=registry) == []
    assert eisv_eras.require_single_era(_t("2026-01-11"), _t("2026-01-19"), path=registry) == []


def test_label_only_entries_do_not_split(registry):
    crossed = eisv_eras.boundaries_crossed(_t("2026-01-14"), _t("2026-01-16"), path=registry)
    assert crossed == []


def test_value_and_population_entries_split(registry):
    crossed = eisv_eras.boundaries_crossed(_t("2026-01-05"), _t("2026-01-30"), path=registry)
    assert [b.id for b in crossed] == ["X1", "X2", "P"]


def test_touching_the_ambiguous_interval_counts_as_crossing(registry):
    # Starts inside X2's interval: neither side of the change.
    assert [b.id for b in eisv_eras.boundaries_crossed(
        _t("2026-01-20T12:30"), _t("2026-01-21"), path=registry)] == ["X2"]
    # Starts once the interval has closed: wholly after.
    assert eisv_eras.boundaries_crossed(
        _t("2026-01-20T13:00"), _t("2026-01-21"), path=registry) == []
    # Ends where the interval opens: wholly before.
    assert eisv_eras.boundaries_crossed(
        _t("2026-01-19"), _t("2026-01-20T11:00"), path=registry) == []


def test_require_single_era_refuses_without_a_reason(registry):
    with pytest.raises(eisv_eras.EraCrossingError) as excinfo:
        eisv_eras.require_single_era(_t("2026-01-18"), _t("2026-01-22"), path=registry)
    assert [b.id for b in excinfo.value.crossed] == ["X2"]
    assert "era_windows" in str(excinfo.value)
    with pytest.raises(eisv_eras.EraCrossingError):
        eisv_eras.require_single_era(_t("2026-01-18"), _t("2026-01-22"), allow_cross="  ", path=registry)


def test_require_single_era_with_a_reason_still_reports_the_crossing(registry):
    crossed = eisv_eras.require_single_era(
        _t("2026-01-18"), _t("2026-01-22"), allow_cross="volume check only", path=registry)
    assert [b.id for b in crossed] == ["X2"]


def test_untimed_entry_splits_any_window_past_the_last_timed_one(tmp_path):
    path = _write_registry(tmp_path, [
        _entry("X1", "2026-01-10T00:00:00Z"),
        _entry("S1", None),
    ])
    assert [b.id for b in eisv_eras.boundaries_crossed(
        _t("2026-01-11"), _t("2026-01-12"), path=path)] == ["S1"]
    assert eisv_eras.boundaries_crossed(_t("2026-01-08"), _t("2026-01-09"), path=path) == []


def test_naive_bounds_are_rejected(registry):
    with pytest.raises(ValueError, match="timezone-aware"):
        eisv_eras.boundaries_crossed(datetime(2026, 1, 1), _t("2026-01-02"), path=registry)
    with pytest.raises(ValueError, match="after its start"):
        eisv_eras.boundaries_crossed(_t("2026-01-02"), _t("2026-01-01"), path=registry)


def test_era_windows_split_and_drop_ambiguous_intervals(registry):
    pieces = eisv_eras.era_windows(_t("2026-01-05"), _t("2026-01-30"), path=registry)
    assert pieces == [
        ("pre-E1", _t("2026-01-05"), _t("2026-01-10")),
        ("X1", _t("2026-01-10"), _t("2026-01-20T11:00")),
        ("X2", _t("2026-01-20T13:00"), _t("2026-01-25")),
        ("P", _t("2026-01-25"), _t("2026-01-30")),
    ]


def test_era_windows_inside_one_era(registry):
    assert eisv_eras.era_windows(_t("2026-01-11"), _t("2026-01-12"), path=registry) == [
        ("X1", _t("2026-01-11"), _t("2026-01-12")),
    ]


def test_live_registry_splits_the_2026_10_15_changes():
    crossed = eisv_eras.boundaries_crossed(_t("2026-10-14"), _t("2026-10-17"))
    assert [b.id for b in crossed if b.effective_at is not None] == ["A1", "I1"]
    # The stop-rule T opens A1's ambiguous interval.
    a1 = next(b for b in crossed if b.id == "A1")
    assert a1.interval == (_t("2026-10-15T13:45"), _t("2026-10-15T14:14:53"))


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------


def test_is_settled():
    assert eisv_eras.is_settled(None) is False
    assert eisv_eras.is_settled({"carried_from": None, "updates_since_change": 1}) is True
    assert eisv_eras.is_settled({"carried_from": "untracked", "updates_since_change": 49}) is False
    assert eisv_eras.is_settled({"carried_from": "A1:abc", "updates_since_change": 50}) is True
    assert eisv_eras.is_settled({"carried_from": "A1:abc", "updates_since_change": 5}, min_updates=5)


# ---------------------------------------------------------------------------
# Stamp and per-monitor track
# ---------------------------------------------------------------------------


def _monitor(update_count: int) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(update_count=update_count))


def test_stamp_is_absent_until_the_track_advances():
    assert eisv_instrument.stamp(_monitor(3)) is None


def test_a_new_monitor_starts_under_the_current_instrument():
    monitor = _monitor(1)
    eisv_instrument.advance(monitor)
    stamp = eisv_instrument.stamp(monitor)
    assert stamp["schema"] == eisv_instrument.STAMP_SCHEMA
    assert stamp["era"] == eisv_eras.current_era_id()
    assert stamp["id"] == f"{stamp['era']}:{stamp['config_hash']}"
    assert stamp["updates_since_change"] == 1
    assert stamp["carried_from"] is None
    assert stamp["build_sha"]


def test_a_monitor_with_prior_state_is_untracked_then_counts():
    monitor = _monitor(400)
    eisv_instrument.advance(monitor)
    eisv_instrument.advance(monitor)
    stamp = eisv_instrument.stamp(monitor)
    assert stamp["carried_from"] == eisv_instrument.UNTRACKED
    assert stamp["updates_since_change"] == 2


def test_an_instrument_change_restarts_the_count_and_names_the_predecessor():
    monitor = _monitor(400)
    setattr(monitor, eisv_instrument.TRACK_ATTR, {"id": "A1:0123456789abcdef", "updates": 900, "carried_from": None})
    eisv_instrument.advance(monitor)
    stamp = eisv_instrument.stamp(monitor)
    assert stamp["carried_from"] == "A1:0123456789abcdef"
    assert stamp["updates_since_change"] == 1


def test_same_instrument_keeps_counting():
    monitor = _monitor(400)
    eisv_instrument.advance(monitor)
    current = getattr(monitor, eisv_instrument.TRACK_ATTR)["id"]
    setattr(monitor, eisv_instrument.TRACK_ATTR, {"id": current, "updates": 70, "carried_from": "A1:x"})
    eisv_instrument.advance(monitor)
    assert eisv_instrument.stamp(monitor)["updates_since_change"] == 71
    assert eisv_instrument.stamp(monitor)["carried_from"] == "A1:x"


def test_a_threshold_override_changes_the_instrument_id():
    from src import runtime_config

    monitor = _monitor(1)
    eisv_instrument.advance(monitor)
    before = eisv_instrument.stamp(monitor)["id"]
    try:
        runtime_config.set_thresholds({"risk_approve_threshold": 0.31})
        eisv_instrument.advance(monitor)
        after = eisv_instrument.stamp(monitor)
    finally:
        runtime_config.clear_overrides()
    assert after["id"] != before
    assert after["carried_from"] == before
    assert after["updates_since_change"] == 1


@pytest.mark.parametrize("flag,value", [
    ("UNITARES_S_SETPOINT", "0"),
    ("UNITARES_PHI_TELEMETRY_ONLY", "0"),
    ("UNITARES_GROUNDING_APPLY", "1"),
    ("UNITARES_FLOOR_BREACH_CAUTION_APPLY", "1"),
    ("UNITARES_I_DYNAMICS", "logistic"),
    ("UNITARES_INTEGRATOR", "euler"),
    ("UNITARES_PARAMS_PROFILE", "v41"),
    ("UNITARES_PARAMS_JSON", '{"alpha": 0.5}'),
    ("UNITARES_SENSOR_COUPLING", "off"),
])
def test_value_affecting_flags_change_the_config_hash(monkeypatch, flag, value):
    monkeypatch.delenv(flag, raising=False)
    eisv_instrument.reset_caches()
    baseline = eisv_instrument.config_hash()
    monkeypatch.setenv(flag, value)
    eisv_instrument.reset_caches()
    assert eisv_instrument.config_hash() != baseline


def test_class_calibration_tables_feed_the_config_hash(monkeypatch):
    from config import governance_config as gc

    baseline = eisv_instrument.config_hash()
    monkeypatch.setitem(gc.HEALTHY_OPERATING_POINT_BY_CLASS, "probe_class", (0.7, 0.8, 0.2))
    eisv_instrument.reset_caches()
    assert eisv_instrument.config_hash() != baseline


# Environment variables the EISV maths modules read, and how each is handled.
# A new UNITARES_* read in these files must be added to one of the two sets:
# COVERED when it can change persisted EISV values (and to _static_config),
# NOT_VALUE_AFFECTING otherwise, with the reason.
_MATHS_MODULES = (
    "governance_core",
    "config/governance_config.py",
    "src/behavioral_assessment.py",
    "src/behavioral_sensor.py",
    "src/behavioral_state.py",
    "src/governance_monitor.py",
    "src/confidence.py",
    "src/eisv_telemetry.py",
    "src/grounding",
)
_COVERED = {
    "UNITARES_S_SETPOINT", "UNITARES_PHI_TELEMETRY_ONLY", "UNITARES_GROUNDING_APPLY",
    "UNITARES_CLASS_CALIBRATION", "UNITARES_PARAMS_PROFILE", "UNITARES_PARAMS_JSON",
    "UNITARES_INTEGRATOR", "UNITARES_I_DYNAMICS", "UNITARES_SENSOR_COUPLING",
    "UNITARES_FLOOR_BREACH_CAUTION_APPLY",
}
_NOT_VALUE_AFFECTING = {
    "UNITARES_GROUNDING_SHADOW": "shadow record only",
    "UNITARES_FLOOR_BREACH_CAUTION_SHADOW": "shadow record only",
    "UNITARES_BASELINE_CACHE_MAXLEN": "cache size",
    "UNITARES_IDENTITY_STRICT": "who may write; recorded as a population era (ID)",
    "UNITARES_SESSION_MIRROR_APPLY": "session binding",
    "UNITARES_SESSION_MIRROR_SHADOW": "session binding",
    "UNITARES_PAUSE_AUTO_EXPIRE_SECONDS": "pause lifetime, not the estimate",
    "UNITARES_IPUA_PIN_CHECK": "identity binding",
    "UNITARES_PREFIX_BIND_FINGERPRINT": "identity binding",
    "UNITARES_SESSION_FINGERPRINT_CHECK": "identity binding",
}


def test_every_env_read_in_the_maths_modules_is_classified():
    pattern = re.compile(r"""(?:getenv|environ\.get|environ\[)\(?\s*["'](UNITARES_[A-Z0-9_]+)""")
    found = set()
    for rel in _MATHS_MODULES:
        root = REPO_ROOT / rel
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            found.update(pattern.findall(path.read_text(encoding="utf-8")))
    unclassified = found - _COVERED - set(_NOT_VALUE_AFFECTING)
    assert not unclassified, (
        f"{sorted(unclassified)} read in the EISV maths modules but not classified. If it "
        "can change persisted values, add it to src/eisv_instrument._static_config and "
        "_COVERED; otherwise add it to _NOT_VALUE_AFFECTING with the reason."
    )


def test_the_resident_roster_feeds_the_config_hash(monkeypatch):
    from src.grounding.class_indicator import RESIDENT_ROSTER_ENV

    monkeypatch.setenv(RESIDENT_ROSTER_ENV, "probe-a")
    eisv_instrument.reset_caches()
    first = eisv_instrument.config_hash()
    monkeypatch.setenv(RESIDENT_ROSTER_ENV, "probe-a,probe-b")
    eisv_instrument.reset_caches()
    assert eisv_instrument.config_hash() != first


def test_advance_is_fail_open(monkeypatch):
    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(eisv_instrument, "config_hash", boom)
    monitor = _monitor(1)
    eisv_instrument.advance(monitor)
    assert eisv_instrument.stamp(monitor) is None


def test_hydration_starts_a_new_count():
    monitor = _monitor(50)
    eisv_instrument.mark_hydrated(monitor)
    assert eisv_instrument.stamp(monitor) is None
    eisv_instrument.advance(monitor)
    stamp = eisv_instrument.stamp(monitor)
    assert stamp["carried_from"] == eisv_instrument.HYDRATED
    assert stamp["updates_since_change"] == 1


@pytest.mark.parametrize("raw", [
    None,
    "I1:x",
    {"id": 3, "updates": 1, "carried_from": None},
    {"id": "I1:x", "updates": "1", "carried_from": None},
    {"id": "I1:x", "updates": 1, "carried_from": 5},
])
def test_restore_track_ignores_malformed_input(raw):
    monitor = _monitor(5)
    eisv_instrument.restore_track(monitor, raw)
    assert not hasattr(monitor, eisv_instrument.TRACK_ATTR)


def test_restore_track_round_trips_the_persisted_block():
    from src.agent_monitor_state import _attach_monitor_transients

    monitor = _monitor(10)
    eisv_instrument.advance(monitor)
    state_data: dict = {}
    _attach_monitor_transients(monitor, state_data)
    persisted = json.loads(json.dumps(state_data))[eisv_instrument.TRACK_STATE_KEY]

    restored = _monitor(10)
    eisv_instrument.restore_track(restored, persisted)
    assert eisv_instrument.stamp(restored) == eisv_instrument.stamp(monitor)


def test_the_monitor_loader_pops_the_track_before_state_parsing():
    source = (REPO_ROOT / "src" / "governance_monitor.py").read_text(encoding="utf-8")
    assert "restore_track(self, data.pop(TRACK_STATE_KEY, None))" in source
    assert "state_data[TRACK_STATE_KEY] = dict(instrument_track)" in source


# ---------------------------------------------------------------------------
# Persistence wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_agent_state_persists_the_stamp():
    from src import agent_storage

    db = MagicMock()
    db.init = AsyncMock()
    db.get_identity = AsyncMock(return_value=SimpleNamespace(identity_id=7))
    db.record_agent_state = AsyncMock(return_value=1)
    stamp = {"schema": eisv_instrument.STAMP_SCHEMA, "era": "I1", "id": "I1:x"}
    agent_storage._db_ready_cache.clear()
    with patch("src.agent_storage.get_db", return_value=db):
        await agent_storage.record_agent_state(
            "agent-1", E=0.7, I=0.8, S=0.2, V=0.0, regime="EXPLORATION",
            coherence=0.5, eisv_instrument=stamp,
        )
        await agent_storage.record_agent_state(
            "agent-1", E=0.7, I=0.8, S=0.2, V=0.0, regime="EXPLORATION", coherence=0.5,
        )
    agent_storage._db_ready_cache.clear()
    first, second = (c.kwargs["state_json"] for c in db.record_agent_state.call_args_list)
    assert first["eisv_instrument"] == stamp
    assert "eisv_instrument" not in second


def test_the_live_update_path_advances_before_saving():
    source = (REPO_ROOT / "src" / "agent_loop_detection.py").read_text(encoding="utf-8")
    assert source.count("eisv_instrument.advance(monitor)") == 2
    for save in ("save_monitor_state(agent_id, monitor)", "await save_monitor_state_async(agent_id, monitor)"):
        index = source.index(save)
        assert "eisv_instrument.advance(monitor)" in source[index - 300:index], save


def test_both_record_sites_pass_the_stamp():
    source = (REPO_ROOT / "src" / "mcp_handlers" / "updates" / "phases.py").read_text(encoding="utf-8")
    assert source.count("eisv_instrument=_eisv_instrument_stamp(monitor)") == 2


# ---------------------------------------------------------------------------
# A reader wired to the guard
# ---------------------------------------------------------------------------


def test_basin_read_refuses_a_crossing_window(capsys):
    from scripts.analysis import basin_conjunct_binding_read as read

    assert read.main(["--window-days", "60"]) == 2
    assert "refused:" in capsys.readouterr().err


def test_current_era_window_start_for_the_live_registry():
    start, stamp_era = eisv_eras.current_era_window_start()
    current = next(b for b in eisv_eras.load_boundaries() if b.id == eisv_eras.current_era_id())
    if current.interval is not None:
        assert (start, stamp_era) == (current.interval[1], None)
    else:
        assert stamp_era == current.id
    assert eisv_eras.boundaries_crossed(
        start, start + timedelta(days=1), stamp_filtered=stamp_era is not None) == []


def test_basin_read_default_start_matches_the_registry():
    from scripts.analysis import basin_conjunct_binding_read as read

    assert read.current_era_start() == eisv_eras.current_era_window_start()[0]


def test_current_era_window_start_timed_and_untimed(tmp_path):
    timed = _write_registry(tmp_path, [_entry("X1", "2026-01-10T00:00:00Z")])
    assert eisv_eras.current_era_window_start(timed) == (_t("2026-01-10"), None)
    untimed = tmp_path / "untimed.json"
    untimed.write_text(json.dumps({"schema": eisv_eras.REGISTRY_SCHEMA, "eras": [
        _entry("X1", "2026-01-10T00:00:00Z", ambiguous_until="2026-01-10T01:00:00Z"),
        _entry("S1", None),
    ]}), encoding="utf-8")
    assert eisv_eras.current_era_window_start(untimed) == (_t("2026-01-10T01:00"), "S1")
    assert eisv_eras.boundaries_crossed(
        _t("2026-01-11"), _t("2026-01-12"), stamp_filtered=True, path=untimed) == []
    with pytest.raises(eisv_eras.EraCrossingError):
        eisv_eras.require_single_era(_t("2026-01-11"), _t("2026-01-12"), path=untimed)


@pytest.mark.asyncio
async def test_basin_read_filters_on_the_stamp_for_an_untimed_era(monkeypatch, capsys):
    from scripts.analysis import basin_conjunct_binding_read as read

    calls = []

    class Conn(_FakeConn):
        async def fetch(self, query, *args):
            calls.append(args)
            return await super().fetch(query, *args)

    monkeypatch.setattr(read.asyncpg, "connect", AsyncMock(return_value=Conn([])))
    start = _t("2026-10-16T01:00")
    assert await read.run("db", start, start + timedelta(hours=1), stamp_era="S1") == 0
    assert calls and all(args[-1] == "S1" for args in calls)
    assert "era `S1` only" in capsys.readouterr().out


def test_require_single_instrument():
    assert eisv_eras.require_single_instrument({None: 40, "I1:a": 10}) == ["I1:a"]
    with pytest.raises(eisv_eras.EraCrossingError, match="2 different EISV instrument ids"):
        eisv_eras.require_single_instrument({"I1:a": 10, "I1:b": 3})
    assert eisv_eras.require_single_instrument(
        {"I1:a": 10, "I1:b": 3}, allow_cross="threshold A/B") == ["I1:a", "I1:b"]
    assert eisv_eras.require_single_instrument({"I1:a": 10, "I1:b": 0}) == ["I1:a"]


class _FakeConn:
    def __init__(self, id_rows):
        self.id_rows = id_rows
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        return self.id_rows if "AS instrument_id" in query else []

    async def fetchrow(self, query, *args):
        return None

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_basin_read_refuses_mixed_stamped_instruments(monkeypatch, capsys):
    from scripts.analysis import basin_conjunct_binding_read as read

    conn = _FakeConn([{"instrument_id": "I1:a", "n": 5}, {"instrument_id": "I1:b", "n": 2}])
    monkeypatch.setattr(read.asyncpg, "connect", AsyncMock(return_value=conn))
    start = _t("2026-10-16T01:00")
    assert await read.run("db", start, start + timedelta(hours=1)) == 2
    assert "2 different EISV instrument ids" in capsys.readouterr().err
    assert len(conn.queries) == 1

    conn = _FakeConn([{"instrument_id": "I1:a", "n": 5}, {"instrument_id": "I1:b", "n": 2}])
    monkeypatch.setattr(read.asyncpg, "connect", AsyncMock(return_value=conn))
    assert await read.run("db", start, start + timedelta(hours=1), allow_cross="A/B") == 0
    out = capsys.readouterr().out
    assert "Pooled across stamped instrument ids" in out and "`I1:b` (2 rows)" in out
