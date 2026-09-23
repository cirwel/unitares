"""Instrument-preservation harness for the EISV check-in path.

First implementation step of ``docs/proposals/active/eisv-core-boundary-v0.md`` (§8.1,
§14). It pins, against the current code and with no seam or flag present:

1. what the estimator computes for fixed check-in sequences (golden file);
2. that every wall-clock read on the estimator path is accounted for, so the
   pin cannot silently start depending on real time;
3. the order of post-update effects, including the early stop when the state
   row cannot be recorded;
4. end to end against PostgreSQL, that the state writer's rows are what the
   registered outcome-grounding read selects and reads at its leads.

A later change that is meant to leave the instrument untouched must pass this
unchanged. A change that is meant to alter EISV behaviour regenerates the golden
file (see ``tests/eisv_preservation_driver.py``) and says so.

Nothing here reads production data or computes an outcome statistic: the
database test uses synthetic rows in ``governance_test`` only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests import eisv_preservation_driver as driver

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Estimator trajectories
# ---------------------------------------------------------------------------


def _hermetic_env() -> dict:
    """Environment for the driver subprocess: no deployment flags leak in."""
    env = driver.strip_config_env(dict(os.environ))
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _run_driver() -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "tests.eisv_preservation_driver"],
        cwd=REPO_ROOT,
        env=_hermetic_env(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout)


def test_estimator_trajectories_match_golden():
    expected = driver.comparable(json.loads(driver.GOLDEN_PATH.read_text(encoding="utf-8")))
    actual = driver.comparable(_run_driver())
    difference = driver.first_difference(expected, actual)
    assert difference is None, (
        f"The pinned EISV instrument changed: {difference}. Treat this as an "
        "instrument change, not a stale fixture. Before the registered "
        "2026-12-01 read's preservation horizon, regenerating the golden file "
        "needs the operator decision in docs/proposals/active/eisv-core-boundary-v0.md "
        "§12.1."
    )


def test_golden_covers_the_scenarios_the_contract_names():
    golden = json.loads(driver.GOLDEN_PATH.read_text(encoding="utf-8"))
    assert golden["schema"] == driver.GOLDEN_SCHEMA
    trajectories = golden["trajectories"]
    assert set(trajectories) == set(driver.SCENARIOS)

    warmup = trajectories["warmup_to_self_relative"]
    assert warmup[0]["behavioral"]["is_baselined"] is False
    assert warmup[-1]["behavioral"]["is_baselined"] is True, "warmup scenario must reach self-relative scoring"

    actions = {step["action"] for steps in trajectories.values() for step in steps}
    assert "pause" in actions, "at least one scenario must pin a pause decision"

    assert {s["behavioral"]["obs_source"] for s in trajectories["embodied_sensor"]} == {"physical"}
    assert {s["behavioral"]["obs_source"] for s in trajectories["behavioral_source_sensor"]} == {"behavioral"}
    assert any(s["trajectory_validation"] for s in warmup), "calibration recording must be exercised"
    assert "provenance" in golden, "the golden file records where it was produced"


def test_an_estimator_change_is_detected():
    """Mutation check on the pipeline, not the comparator: nudge one behavioral
    observation inside the estimator and the driver output must move."""
    import logging
    from unittest.mock import patch

    from src.behavioral_state import BehavioralEISV

    original_update = BehavioralEISV.update

    def nudged(self, E, I, S, *args, **kwargs):
        return original_update(self, E + 0.01, I, S, *args, **kwargs)

    logging.disable(logging.CRITICAL)
    try:
        baseline = driver.comparable(driver.run_all())
        with patch.object(BehavioralEISV, "update", nudged):
            mutated = driver.comparable(driver.run_all())
    finally:
        logging.disable(logging.NOTSET)
    assert driver.first_difference(baseline, mutated) is not None


def test_comparator_reports_a_perturbed_value():
    golden = json.loads(driver.GOLDEN_PATH.read_text(encoding="utf-8"))
    for mutate in (
        lambda g: g["trajectories"]["warmup_to_self_relative"][30].__setitem__("risk_score", 0.123),
        lambda g: g["trajectories"]["high_risk_first_checkins"][0].__setitem__("action", "proceed"),
        lambda g: g["trajectories"]["saturating_gap"].pop(),
    ):
        perturbed = json.loads(json.dumps(golden))
        mutate(perturbed)
        assert driver.first_difference(golden, perturbed) is not None


def test_process_global_state_is_declared_and_resettable():
    import importlib

    for module_name, attribute in driver.PROCESS_GLOBAL_STATE:
        value = getattr(importlib.import_module(module_name), attribute)
        assert hasattr(value, "clear"), f"{module_name}.{attribute} can no longer be reset"


def test_driver_is_deterministic_in_process():
    import logging

    logging.disable(logging.CRITICAL)
    try:
        first = driver.run_all()
        second = driver.run_all()
    finally:
        logging.disable(logging.NOTSET)
    assert driver.first_difference(first, second) is None


# ---------------------------------------------------------------------------
# 2. Clock-source inventory, checked at runtime
# ---------------------------------------------------------------------------

_REPO_PACKAGES = ("src", "governance_core", "config")


def _trace_real_clock_calls() -> set[tuple[str, str]]:
    """Run the scenarios and record every real clock builtin called from repo code.

    Injected clocks are Python functions, so they never appear as C calls; what
    this records is exactly the clock reads the driver does not replace.
    """
    import datetime as real_datetime
    import logging
    import time as real_time

    clock_builtins = {
        real_time.monotonic, real_time.monotonic_ns, real_time.time, real_time.time_ns,
        real_time.perf_counter, real_time.perf_counter_ns,
    }
    datetime_classes = (real_datetime.datetime, real_datetime.date)
    found: set[tuple[str, str]] = set()

    def profiler(frame, event, arg):
        if event != "c_call":
            return
        is_clock = arg in clock_builtins or (
            getattr(arg, "__name__", None) in {"now", "utcnow", "today"}
            and getattr(arg, "__self__", None) in datetime_classes
        )
        if not is_clock:
            return
        module = frame.f_globals.get("__name__", "")
        if module.split(".")[0] in _REPO_PACKAGES:
            found.add((module, arg.__name__))

    logging.disable(logging.CRITICAL)
    try:
        driver.run_all()  # import everything first, so imports are not traced
        sys.setprofile(profiler)
        try:
            driver.run_all()
        finally:
            sys.setprofile(None)
    finally:
        logging.disable(logging.NOTSET)
    return found


def test_every_real_clock_read_during_the_scenarios_is_declared():
    unaccounted = sorted(_trace_real_clock_calls() - set(driver.UNINJECTED_CLOCK_READS))
    assert not unaccounted, (
        "Real clock reads made from repository code while the preservation "
        f"scenarios ran, neither injected nor declared: {unaccounted}. Inject them "
        "in CLOCK_SOURCES (tests/eisv_preservation_driver.py) or declare why they "
        "cannot affect the pinned outputs."
    )


def test_declared_clock_sources_still_exist():
    import importlib

    for module_name, attribute, kind in driver.CLOCK_SOURCES:
        module = importlib.import_module(module_name)
        assert hasattr(module, attribute), f"{module_name} has no attribute {attribute}"
        assert kind in {"datetime", "time"}


# ---------------------------------------------------------------------------
# 3. Post-update effect order
# ---------------------------------------------------------------------------

POST_UPDATE_ORDER = (
    "_post_update_health_and_baselines",
    "_post_update_cirs_and_drift",
    "_post_update_record_state",
    "_post_update_save_baseline",
    "_post_update_auto_outcome",
    "_post_update_trajectory",
    "_post_update_phase5_evidence",
    "_r2_post_update_hook",
)


def _patch_effects(monkeypatch, calls, *, record_state_result):
    from src.mcp_handlers.updates import phases

    for name in POST_UPDATE_ORDER:
        async def _effect(ctx, _name=name):
            calls.append(_name)
            return record_state_result if _name == "_post_update_record_state" else None

        monkeypatch.setattr(phases, name, _effect)
    return phases


@pytest.mark.asyncio
async def test_post_update_effects_run_in_pinned_order(monkeypatch):
    calls: list[str] = []
    phases = _patch_effects(monkeypatch, calls, record_state_result=True)
    await phases.execute_post_update_effects(object())
    assert tuple(calls) == POST_UPDATE_ORDER


@pytest.mark.asyncio
async def test_post_update_effects_stop_when_state_row_is_not_recorded(monkeypatch):
    calls: list[str] = []
    phases = _patch_effects(monkeypatch, calls, record_state_result=False)
    await phases.execute_post_update_effects(object())
    assert tuple(calls) == POST_UPDATE_ORDER[:3]


# ---------------------------------------------------------------------------
# 4. Writer -> registered read, against PostgreSQL
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    try:
        from tests.test_db_utils import can_connect_to_test_db
    except Exception:
        return False
    return can_connect_to_test_db()


@pytest.fixture
def preservation_db(request):
    """The live test database, required in CI.

    A silent skip would turn the only join pin into a green no-op when the CI
    database service is down, so under GitHub Actions an unavailable database
    fails instead.
    """
    if not _db_available():
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("governance_test database unavailable in CI; the registered-join pin cannot run")
        pytest.skip("governance_test database not available")
    return request.getfixturevalue("live_postgres_backend")


@pytest.mark.asyncio
async def test_state_writer_rows_are_what_the_registered_read_selects(preservation_db, monkeypatch):
    """The registered read joins each outcome to the latest non-synthetic state
    row at or before ``outcome.ts - lead``. Pin that selection, the boundary,
    synthetic-row exclusion, and the E/I/S/V/risk mapping from the real writer
    to the read's fields.

    Scope limits, stated so this is not over-read: timestamps are set
    explicitly rather than produced by a real check-in, the outcome row is a
    direct insert, and ``fetch_rows`` runs without the registered cohort's
    anchor, fixture-rule, and harness-lane filters.
    """
    from scripts.analysis.eisv_skeptic_report import fetch_rows
    from src import agent_storage
    from tests.test_db_utils import TEST_DB_URL

    db = preservation_db
    async with db.acquire() as conn:
        database = await conn.fetchval("SELECT current_database()")
    assert database == "governance_test", f"refusing to run the read against {database!r}"
    assert TEST_DB_URL.rsplit("/", 1)[-1] == "governance_test"

    monkeypatch.setattr(agent_storage, "get_db", lambda: db)

    async def _ready():
        return None

    monkeypatch.setattr(agent_storage, "_ensure_db_ready", _ready)

    agent_id = f"preservation-{uuid.uuid4()}"
    outcome_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    try:
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key')", agent_id)
            identity_id = await conn.fetchval(
                "INSERT INTO core.identities (agent_id, api_key_hash) VALUES ($1, 'test-hash') RETURNING identity_id",
                agent_id,
            )

        # (minutes before the outcome, E, I, S, V, risk)
        states = [
            (90, 0.61, 0.71, 0.21, 0.01, 0.11),
            (30, 0.62, 0.72, 0.22, 0.02, 0.12),  # exactly at the lead-30 boundary
            (10, 0.63, 0.73, 0.23, 0.03, 0.13),
        ]
        for minutes, E, I, S, V, risk in states:
            state_id = await agent_storage.record_agent_state(
                agent_id,
                E=E, I=I, S=S, V=V,
                regime="STABLE",
                coherence=0.5,
                risk_score=risk,
                phi=0.0,
                verdict="safe",
                action="approve",
            )
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE core.agent_state SET recorded_at = $1 WHERE state_id = $2",
                    outcome_ts - timedelta(minutes=minutes),
                    state_id,
                )

        async with db.acquire() as conn:
            # A newer synthetic row the read must ignore.
            await conn.execute(
                """
                INSERT INTO core.agent_state
                    (identity_id, recorded_at, entropy, integrity, volatility, coherence,
                     regime, state_json, synthetic, risk_score)
                VALUES ($1, $2, 0.99, 0.99, 0.99, 0.5, 'nominal', '{"E": 0.99}'::jsonb, true, 0.99)
                """,
                identity_id,
                outcome_ts - timedelta(minutes=1),
            )
            await conn.execute(
                """
                INSERT INTO audit.outcome_events (ts, agent_id, outcome_type, outcome_score, is_bad, detail)
                VALUES ($1, $2, 'test_failed', 0.0, true, '{}'::jsonb)
                """,
                outcome_ts,
                agent_id,
            )

        async def _selected(lead_minutes: float):
            rows = await fetch_rows(
                TEST_DB_URL,
                window_days=1,
                lead_minutes=lead_minutes,
                outcome_types=["test_failed"],
                as_of=outcome_ts + timedelta(seconds=1),
                exclude_controlled_fixtures=False,
                include_identity_metadata=False,
            )
            mine = [row for row in rows if row.agent_id == agent_id]
            assert len(mine) == 1
            return mine[0]

        at_lead_0 = await _selected(0)
        assert (at_lead_0.prior_e, at_lead_0.prior_i, at_lead_0.prior_s, at_lead_0.prior_v) == pytest.approx(
            (0.63, 0.73, 0.23, 0.03), abs=1e-6
        )
        assert at_lead_0.prior_risk == pytest.approx(0.13, abs=1e-6)
        assert at_lead_0.prior_state_age_seconds == pytest.approx(600, abs=1)

        at_lead_30 = await _selected(30)
        assert (at_lead_30.prior_e, at_lead_30.prior_i, at_lead_30.prior_s, at_lead_30.prior_v) == pytest.approx(
            (0.62, 0.72, 0.22, 0.02), abs=1e-6
        ), "a state row exactly at outcome.ts - lead is selected (<=)"
        assert at_lead_30.prior_risk == pytest.approx(0.12, abs=1e-6)
    finally:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM audit.outcome_events WHERE agent_id = $1", agent_id)
            await conn.execute(
                "DELETE FROM core.agent_state WHERE identity_id IN "
                "(SELECT identity_id FROM core.identities WHERE agent_id = $1)",
                agent_id,
            )
            await conn.execute("DELETE FROM core.identities WHERE agent_id = $1", agent_id)
            await conn.execute("DELETE FROM core.agents WHERE id = $1", agent_id)
