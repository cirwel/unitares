"""The basin-gate validator must be able to FAIL.

`validate_basin_gate.py` certifies that the #689 basin gate stops the #686
false-pause class without masking genuine danger. It printed
"PASS — acceptance criteria met" with the gate deleted: every assertion was
already satisfied by the `before` column, which the script computes precisely to
represent the pre-gate world. The two rows where the gate changed a verdict were
the two carrying no assertion.

That is the antipattern from `docs/operations/positive-control-validity-2026-08-23.md`
— an instrument whose verdict cannot distinguish treatment from no treatment.
These tests pin the repair by driving both worlds.
"""

from __future__ import annotations

import pytest

import src.behavioral_assessment as ba
import scripts.analysis.validate_basin_gate as validator


def _delete_the_gate(monkeypatch):
    """Replace the basin gate with the constant-1.0 no-op.

    Arithmetically identical to removing the `* gate[...]` multipliers, i.e. the
    world in which #689 was never implemented.
    """
    monkeypatch.setattr(
        ba,
        "_basin_health_gate",
        lambda state: {"low_E": 1.0, "low_I": 1.0, "high_S": 1.0, "high_V": 1.0},
    )


def test_sweep_passes_with_the_gate_present():
    assert validator.run_sweep() is True


def test_sweep_fails_when_the_gate_is_deleted(monkeypatch):
    """The test this file exists for. Before the repair this returned True."""
    _delete_the_gate(monkeypatch)
    assert validator.run_sweep() is False


def test_trace_cases_pass_with_the_gate_present():
    assert validator.run_trace_cases() is True


def test_at_least_one_case_asserts_on_the_treatment_effect():
    """A `de_escalates` case is the only kind the gate's absence can fail.

    Guards against the expectations silently reverting to `none`, which is how
    the effect became unasserted in the first place.
    """
    import inspect

    source = inspect.getsource(validator.run_sweep)
    assert source.count('"de_escalates"') >= 2


async def _fake_live_skip(dsn, limit):
    """Stand-in for `run_live` that skips without touching the network."""
    return True, 0, "no database connection"


async def _fake_live_pass(dsn, limit):
    """Stand-in for `run_live` that examined rows and found no masking."""
    return True, 42, None


async def _fake_live_silent_zero(dsn, limit):
    """A skip path that forgot to populate the reason string."""
    return True, 0, None


class _FakeConn:
    """Minimal asyncpg connection stand-in. `fetch` returns rows or raises."""

    def __init__(self, rows=None, fetch_error=None):
        self._rows = rows if rows is not None else []
        self._fetch_error = fetch_error
        self.closed = False

    async def fetch(self, *args, **kwargs):
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._rows

    async def close(self):
        self.closed = True


class _FakeAsyncpg:
    """Stands in for the `asyncpg` module inside `run_live`'s local import."""

    def __init__(self, conn=None, connect_error=None):
        self._conn = conn
        self._connect_error = connect_error

    async def connect(self, *args, **kwargs):
        if self._connect_error is not None:
            raise self._connect_error
        return self._conn


def _install_asyncpg(monkeypatch, fake):
    """Make `import asyncpg` inside `run_live` resolve to `fake` (or fail)."""
    import sys

    monkeypatch.setitem(sys.modules, "asyncpg", fake)
    # A DSN must be present, or `run_live` calls connect() with no arguments and
    # asyncpg's real default-resolution would be the thing under test instead of
    # the branch we mean to reach.
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/hermetic")


@pytest.mark.parametrize(
    "fake,expected_reason",
    [
        (None, "asyncpg not installed"),
        (_FakeAsyncpg(connect_error=OSError("refused")), "no database connection"),
        (_FakeAsyncpg(conn=_FakeConn(fetch_error=RuntimeError("boom"))), "query failed"),
        (_FakeAsyncpg(conn=_FakeConn(rows=[])), "no eligible rows"),
    ],
    ids=["no-asyncpg", "no-connection", "query-failed", "no-rows"],
)
def test_every_real_skip_branch_returns_a_reason(monkeypatch, fake, expected_reason):
    """Drive the REAL `run_live` down each of its four skip paths.

    Every early exit returned a bare True, which `main` ANDed into an
    unqualified "PASS — acceptance criteria met". This pins that each one now
    reports (ok=True, examined=0, reason) — a skip is not a failure, but it is
    not a pass either.

    Hermetic without patching the function under test: `asyncpg` is injected
    into `sys.modules`, so `run_live`'s own local import picks up a fake and no
    socket is opened. `sys.modules["asyncpg"] = None` is how CPython spells
    "this import fails", which reaches the not-installed branch even where
    asyncpg IS installed.
    """
    import asyncio

    _install_asyncpg(monkeypatch, fake)
    ok, examined, skip_reason = asyncio.run(validator.run_live(None, 10))

    assert ok is True          # a skip is not a failure
    assert examined == 0
    assert skip_reason == expected_reason   # ...but it is not a pass either


def test_real_run_live_closes_the_connection_on_a_query_failure(monkeypatch):
    """The query-failed path must not leak the connection it opened."""
    import asyncio

    conn = _FakeConn(fetch_error=RuntimeError("boom"))
    _install_asyncpg(monkeypatch, _FakeAsyncpg(conn=conn))
    asyncio.run(validator.run_live(None, 10))

    assert conn.closed is True


def test_main_exits_2_when_a_requested_arm_skips(capsys, monkeypatch):
    """Unassessed must be distinguishable from passed by exit status alone.

    EVALUATION_INDEX.md documents this script's contract as "Console PASS/FAIL
    + exit", and eisv-basin-health-gating-v0.md names `--db` as a pre-merge
    operator step, so a consumer reading only the status must not see success
    from a run that examined nothing.
    """
    monkeypatch.setattr(validator, "run_live", _fake_live_skip)
    monkeypatch.setattr("sys.argv", ["validate_basin_gate.py", "--db"])
    rc = validator.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert "UNASSESSED" in out
    assert "live arm SKIPPED" in out
    assert "PASS — acceptance criteria met" not in out


def test_main_trusts_the_count_over_the_reason_string(capsys, monkeypatch):
    """examined==0 is unassessed even if no reason string was populated.

    The count is the ground truth; the string is a report of it. A future skip
    path that returns 0 without a reason must not be readable as a pass.
    """
    monkeypatch.setattr(validator, "run_live", _fake_live_silent_zero)
    monkeypatch.setattr("sys.argv", ["validate_basin_gate.py", "--db"])
    rc = validator.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert "UNASSESSED" in out
    assert "PASS — acceptance criteria met" not in out


def test_main_exits_0_when_the_live_arm_actually_examined_rows(capsys, monkeypatch):
    """The other direction: a live arm that ran must give an unqualified pass."""
    monkeypatch.setattr(validator, "run_live", _fake_live_pass)
    monkeypatch.setattr("sys.argv", ["validate_basin_gate.py", "--db"])
    rc = validator.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS — acceptance criteria met" in out
    assert "UNASSESSED" not in out


def test_main_gives_an_unqualified_pass_only_for_the_synthetic_arms(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["validate_basin_gate.py"])
    rc = validator.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS — acceptance criteria met" in out
