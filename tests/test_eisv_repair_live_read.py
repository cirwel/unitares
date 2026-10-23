"""The repair live-read implements its declared rule, on synthetic rows only.

docs/evaluations/eisv-i-rest-live-read/rule-v0.md fixes the windows, filters,
strata, floors and outcomes before the read. These tests pin the script to
that text without touching the database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.analysis import eisv_repair_live_read as read
from src import eisv_eras

UTC = timezone.utc
POST_START = datetime(2026, 10, 16, 2, 59, 50, tzinfo=UTC)
POST_END = datetime(2026, 10, 22, 14, 27, 20, tzinfo=UTC)
BASE_START = datetime(2026, 10, 5, 22, 6, 59, tzinfo=UTC)
BASE_END = datetime(2026, 10, 15, 13, 45, tzinfo=UTC)
READ_AT = datetime(2026, 10, 23, 1, 0, tzinfo=UTC)
ROSTER = frozenset({"resident-a"})
STAMP = {"id": read.POST_INSTRUMENT_ID, "era": "RK1", "carried_from": None, "updates_since_change": 1}


def _row(
    identity: str,
    minute: int,
    *,
    start: datetime = POST_START,
    action: str = "approve",
    verdict: str = "safe",
    sensor: str = "behavioral",
    driver: str = "behavioral_assessment",
    stamp=STAMP,
    e: float | None = 0.7,
    label: str | None = None,
    tags: tuple[str, ...] = ("ephemeral",),
    first_seen: datetime | None = None,
) -> read.Row:
    at = start + timedelta(minutes=minute)
    return read.Row(
        identity_id=identity,
        recorded_at=at,
        sensor=sensor,
        verdict=verdict,
        action=action,
        verdict_source=driver,
        stamp=stamp,
        e=e,
        label=label,
        tags=tags,
        first_seen=first_seen or start,
    )


def _identity_rows(identity: str, n: int, approve: int, **kw) -> list[read.Row]:
    return [
        _row(identity, i, action="approve" if i < approve else "guide", **kw) for i in range(n)
    ]


def _windows():
    return read.resolve_windows(READ_AT)


def test_windows_are_the_rules_eras():
    post, baseline = _windows()
    assert (post.era, post.start, post.end, post.stamped) == ("RK1", POST_START, POST_END, True)
    assert (baseline.era, baseline.start, baseline.end, baseline.stamped) == (
        "ID",
        BASE_START,
        BASE_END,
        False,
    )


def test_neither_window_crosses_an_instrument_change():
    for window in _windows():
        assert eisv_eras.check_window(window.start, window.end) is not None


def test_baseline_settles_without_stamps():
    """Stamping began with I1, so is_settled would drop every baseline row."""
    _, baseline = _windows()
    carried = [
        _row("old", i, start=BASE_START, stamp=None, first_seen=BASE_START - timedelta(days=3))
        for i in range(60)
    ]
    fresh = [_row("new", i, start=BASE_START, stamp=None) for i in range(5)]

    kept, excluded, stamped = read.settled_rows(carried + fresh, baseline)

    assert not any(eisv_eras.is_settled(r.stamp) for r in carried)
    assert excluded == read.SETTLE_ROWS
    assert sum(1 for r in kept if r.identity_id == "old") == 60 - read.SETTLE_ROWS
    assert sum(1 for r in kept if r.identity_id == "new") == 5
    assert stamped == 0


def test_post_era_settles_on_the_stamp():
    post, _ = _windows()
    carried = {**STAMP, "carried_from": "I1", "updates_since_change": 3}
    rows = [_row("a", 0), _row("b", 1, stamp=carried)]

    kept, excluded, _ = read.settled_rows(rows, post)

    assert [r.identity_id for r in kept] == ["a"]
    assert excluded == 1


def test_decides_on_the_pooled_share_the_replay_used():
    """Review case: shares [0, 0, 1, 1, 1] pool to 60% while their median is 100%."""
    rows = []
    for idx, approve in enumerate([0, 0, 20, 20, 20]):
        rows += _identity_rows(f"id{idx}", 20, approve)
    stats = read.stratum_stats(rows)

    assert stats.pooled_share == pytest.approx(0.6)
    assert stats.identity_p50_share == 1.0
    outcome, _ = read.stratum_outcome(stats, read.stratum_stats(rows))
    assert outcome == "CONSISTENT"


def _passing(approve_each: int, n: int = 25, ids: int = 5, **kw) -> read.StratumStats:
    rows = []
    for idx in range(ids):
        rows += _identity_rows(f"id{idx}", n, approve_each, **kw)
    return read.stratum_stats(rows)


@pytest.mark.parametrize(
    "approve_each, expected",
    [(5, "BELOW"), (15, "CONSISTENT"), (22, "ABOVE, unattributed")],
)
def test_outcome_against_the_pooled_range(approve_each, expected):
    baseline = _passing(5)
    assert read.stratum_outcome(_passing(approve_each), baseline)[0] == expected


def test_above_is_composition_when_the_baseline_was_already_high():
    assert read.stratum_outcome(_passing(24), _passing(20))[0] == "ABOVE, composition"


def test_above_is_loop_consistent_when_behavioral_e_rose():
    rows = []
    for idx in range(5):
        rows += [_row(f"id{idx}", i, e=0.5 + i / 100) for i in range(25)]
    assert read.stratum_outcome(read.stratum_stats(rows), _passing(5))[0] == "ABOVE, loop-consistent"


def test_per_stratum_endpoints_replace_the_range():
    outcome, _ = read.stratum_outcome(_passing(22), _passing(5), endpoints=(0.85, 0.95))
    assert outcome == "CONSISTENT"


@pytest.mark.parametrize(
    "post, why",
    [
        (lambda: _passing(15, ids=4, n=30), "identities"),
        (lambda: _passing(5, ids=5, n=15), "safe rows"),
    ],
)
def test_floors_make_a_stratum_inconclusive(post, why):
    outcome, reasons = read.stratum_outcome(post(), _passing(5))
    assert outcome == "INCONCLUSIVE"
    assert any(why in r for r in reasons)


def test_one_identity_above_a_majority_is_inconclusive():
    rows = _identity_rows("heavy", 120, 60)
    for idx in range(5):
        rows += _identity_rows(f"id{idx}", 20, 10)
    outcome, reasons = read.stratum_outcome(read.stratum_stats(rows), _passing(5))
    assert outcome == "INCONCLUSIVE"
    assert any("one identity holds" in r for r in reasons)


def test_floors_bind_in_the_baseline_era_too():
    """Review case: a strong post era over a one-row baseline is not ABOVE, composition."""
    thin_baseline = read.stratum_stats([_row("b", 0, start=BASE_START, stamp=None)])
    outcome, reasons = read.stratum_outcome(_passing(25), thin_baseline)
    assert outcome == "INCONCLUSIVE"
    assert reasons and all(r.startswith("baseline era:") for r in reasons)


def test_a_stamped_baseline_row_of_any_sensor_fails_the_read():
    _, baseline = _windows()
    rows = [_row("lumen", 0, start=BASE_START, sensor="physical")]
    rows += [_row(f"b{i}", j, start=BASE_START, stamp=None) for i in range(5) for j in range(25)]
    assert read.read_era(rows, baseline, ROSTER).stamped_rows == 1


def test_markdown_reports_both_eras_statistics():
    post, baseline = _windows()
    post_era = read.read_era([r for i in range(5) for r in _identity_rows(f"p{i}", 25, 15)], post, ROSTER)
    base_era = read.read_era(
        [r for i in range(6) for r in _identity_rows(f"b{i}", 30, 6, start=BASE_START, stamp=None)],
        baseline,
        ROSTER,
    )
    text = read.render(read.build_report(post_era, base_era, instrument_ids=[read.POST_INSTRUMENT_ID]))
    base_line = next(line for line in text.splitlines() if "| B |" in line)
    assert "| 20.0% | 180 | 6 | 6 | 30 | 16.7% | 20.0% |" in base_line
    post_line = next(line for line in text.splitlines() if "| P |" in line)
    assert "| 60.0% | 125 | 5 | 5 | 25 | 20.0% | 60.0% |" in post_line


def test_partial_endpoints_keep_the_range_only_warning_where_they_fall_back():
    post, baseline = _windows()
    post_rows = [r for i in range(5) for r in _identity_rows(f"p{i}", 25, 5)]
    post_rows += [r for i in range(5) for r in _identity_rows(f"q{i}", 25, 5, driver="phi_cold_start")]
    base_rows = [
        r
        for i in range(5)
        for drv in ("behavioral_assessment", "phi_cold_start")
        for r in _identity_rows(f"b{i}{drv}", 25, 5, start=BASE_START, stamp=None, driver=drv)
    ]
    report = read.build_report(
        read.read_era(post_rows, post, ROSTER),
        read.read_era(base_rows, baseline, ROSTER),
        endpoints={"behavioral_assessment/ephemeral": [0.1, 0.3]},
        instrument_ids=[read.POST_INSTRUMENT_ID],
    )
    sources = {s["stratum"]: (s["endpoint_source"], s["outcome"]) for s in report["strata"]}
    assert sources == {
        "behavioral_assessment/ephemeral": ("per_stratum", "CONSISTENT"),
        "other/ephemeral": ("range_only", "BELOW"),
    }
    assert report["range_only"] is True
    text = read.render(report)
    assert "Range-only for `other/ephemeral`" in text
    assert "behavioral_assessment/ephemeral`:" not in text.split("Range-only for")[1]


def test_empty_baseline_stratum_is_inconclusive():
    outcome, reasons = read.stratum_outcome(_passing(15), None)
    assert outcome == "INCONCLUSIVE"
    assert "baseline" in reasons[0]


def test_strata_cross_driver_and_class_and_exclude_other_sensors():
    post, _ = _windows()
    rows = (
        _identity_rows("e1", 3, 3)
        + [_row("e2", 0, driver="phi_cold_start")]
        + [_row("res", 0, label="resident-a", tags=("persistent", "autonomous"))]
        + [_row("lumen", 0, sensor="physical")]
        + [_row("x", 0, sensor=None)]
        + [_row("e3", 0, verdict="caution")]
    )

    result = read.read_era(rows, post, ROSTER)

    assert set(result.strata) == {
        ("behavioral_assessment", "ephemeral"),
        ("other", "ephemeral"),
        ("behavioral_assessment", "resident-a"),
    }
    assert result.other_sensor_rows == {"physical": 1, "unrecorded": 1}


def test_a_resident_stratum_is_inconclusive_by_construction():
    rows = _identity_rows("res", 400, 300, label="resident-a")
    stats = read.stratum_stats(rows)
    outcome, reasons = read.stratum_outcome(stats, stats)
    assert stats.identities == 1
    assert outcome == "INCONCLUSIVE"


def test_stamped_baseline_rows_or_a_foreign_instrument_make_every_stratum_inconclusive():
    post, baseline = _windows()
    post_era = read.read_era(
        [r for i in range(5) for r in _identity_rows(f"p{i}", 25, 15)], post, ROSTER
    )
    base_rows = [
        r
        for i in range(5)
        for r in _identity_rows(f"b{i}", 25, 5, start=BASE_START, stamp=None)
    ]
    clean = read.build_report(post_era, read.read_era(base_rows, baseline, ROSTER),
                              instrument_ids=[read.POST_INSTRUMENT_ID])
    assert [s["outcome"] for s in clean["strata"]] == ["CONSISTENT"]
    assert clean["range_only"] is True

    base_rows[0] = _row("b0", 0, start=BASE_START)  # carries a stamp
    stamped = read.build_report(post_era, read.read_era(base_rows, baseline, ROSTER),
                                instrument_ids=[read.POST_INSTRUMENT_ID])
    assert stamped["strata"][0]["outcome"] == "INCONCLUSIVE"

    foreign = read.build_report(post_era, read.read_era(base_rows[1:], baseline, ROSTER),
                                instrument_ids=["RK1:ffffffffffffffff"])
    assert foreign["strata"][0]["outcome"] == "INCONCLUSIVE"


def test_refuses_before_the_rules_date(capsys):
    assert read.main([], now=datetime(2026, 10, 22, 23, 59, tzinfo=UTC)) == 2
    assert "on or after" in capsys.readouterr().err


def test_refuses_without_a_resident_roster(monkeypatch, capsys):
    monkeypatch.setattr(read.class_indicator, "load_resident_labels", lambda: frozenset())
    assert read.main([], now=READ_AT) == 2
    assert "UNITARES_RESIDENTS" in capsys.readouterr().err


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if "count(*)" in sql:
            self._result = [(read.POST_INSTRUMENT_ID, 125)]
        else:
            self.conn.fetches.append(params["start"])
            self._result = self.conn.rows_by_start[params["start"]]

    def fetchall(self):
        return self._result


class _Conn:
    def __init__(self, rows_by_start):
        self.rows_by_start = rows_by_start
        self.fetches = []
        self.session = None
        self.closed = False

    def set_session(self, **kwargs):
        self.session = kwargs

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


def _db_record(row: read.Row) -> tuple:
    return (
        row.identity_id,
        row.recorded_at,
        row.sensor,
        row.verdict,
        row.action,
        row.verdict_source,
        row.stamp,
        None if row.e is None else str(row.e),
        row.label,
        list(row.tags),
        row.first_seen,
    )


def test_main_reads_both_eras_in_one_read_only_snapshot(monkeypatch, capsys):
    monkeypatch.setattr(read.class_indicator, "load_resident_labels", lambda: ROSTER)
    post_rows = [r for i in range(5) for r in _identity_rows(f"p{i}", 25, 15)]
    base_rows = [
        r for i in range(5) for r in _identity_rows(f"b{i}", 25, 5, start=BASE_START, stamp=None)
    ]
    conn = _Conn({POST_START: [_db_record(r) for r in post_rows],
                  BASE_START: [_db_record(r) for r in base_rows]})

    assert read.main(["--json"], now=READ_AT, connect=lambda url: conn) == 0

    assert conn.session == {"isolation_level": "REPEATABLE READ", "readonly": True}
    assert conn.fetches == [POST_START, BASE_START]
    assert conn.closed
    out = capsys.readouterr().out
    assert '"outcome": "CONSISTENT"' in out
    assert read.POST_INSTRUMENT_ID in out
