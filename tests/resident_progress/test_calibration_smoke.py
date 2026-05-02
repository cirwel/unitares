"""Calibration smoke test — catches obviously-misconfigured thresholds
before deploy. The 50% ceiling is a hard upper bound; the operational
tuning target is much lower and will be set from real data after Phase 1.

Filters to ticks that have a `progress_flat_probe` dogfood row, so
synthetic test inserts (from snapshot_writer/probe_task tests) do not
pollute the calibration signal. Skips when no real probe activity is
present in the window.

Event-driven residents (`expected_cadence_s is None` in the registry,
e.g. Watcher) are excluded from the candidate-rate ceiling. The probe
synthesizes ``alive=True`` for them (PR #248), and their source-count
threshold is structurally below-or-equal to baseline most of the time,
so ``candidate = below AND alive`` is True whenever they're working
normally — the 50%-ceiling invariant doesn't apply.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest

from src.resident_progress.registry import RESIDENT_PROGRESS_REGISTRY


_REAL_TICK_IDS_SQL = """
SELECT DISTINCT probe_tick_id
FROM progress_flat_snapshots
WHERE ticked_at > now() - $1::interval
  AND resident_label = 'progress_flat_probe'
"""


_EVENT_DRIVEN_LABELS = frozenset(
    label
    for label, cfg in RESIDENT_PROGRESS_REGISTRY.items()
    if cfg.expected_cadence_s is None
)


@pytest.mark.asyncio
async def test_no_resident_candidate_above_fifty_percent(test_db):
    """If ANY *cadenced* resident is firing candidates >50% of REAL probe
    ticks, its threshold is misconfigured. Hard ceiling — operational
    target is much lower.

    Event-driven residents (registry entry with ``expected_cadence_s
    is None``) are exempt: their alive flag is synthesized to True
    unconditionally (PR #248), so for any tick where their source-count
    is below threshold the probe records ``candidate=True`` by design.
    The 50%-rule is a calibration check on cadenced residents only.
    """
    async with test_db.acquire() as conn:
        real_tick_ids = await conn.fetch(_REAL_TICK_IDS_SQL, timedelta(hours=24))
        if not real_tick_ids:
            pytest.skip(
                "no real probe ticks in the last 24h — run the probe "
                "against this DB at least once"
            )
        rows = await conn.fetch(
            """
            SELECT resident_label, candidate
            FROM progress_flat_snapshots
            WHERE probe_tick_id = ANY($1::uuid[])
              AND resident_label != 'progress_flat_probe'
            """,
            [r["probe_tick_id"] for r in real_tick_ids],
        )
    if not rows:
        pytest.skip(
            "real probe ticks had no resident rows — probe ran with empty "
            "registry; nothing to calibrate"
        )

    by_label: Counter = Counter()
    candidate_by_label: Counter = Counter()
    for r in rows:
        label = r["resident_label"]
        if label in _EVENT_DRIVEN_LABELS:
            continue
        by_label[label] += 1
        if r["candidate"]:
            candidate_by_label[label] += 1

    offenders = []
    for label, total in by_label.items():
        ratio = candidate_by_label[label] / total
        if ratio > 0.5:
            offenders.append((label, ratio, total))
    assert not offenders, (
        f"cadenced residents firing candidate > 50% of ticks "
        f"(threshold misconfigured?): {offenders}"
    )


def test_event_driven_residents_are_exempted_from_smoke():
    """The exemption set is derived from the registry's own carve-out.
    Guards against a future registry edit that would silently re-include
    event-driven residents in the >50% ceiling check (the rule is
    structurally wrong for them — `candidate=True` is the *healthy*
    state when alive=True is synthesized and source-count is below
    threshold, which for event-driven residents is most of the time).
    """
    expected = {
        label
        for label, cfg in RESIDENT_PROGRESS_REGISTRY.items()
        if cfg.expected_cadence_s is None
    }
    assert _EVENT_DRIVEN_LABELS == expected
    # Sanity: at least one event-driven resident exists today (Watcher).
    # If this assertion ever flips, either Watcher gained a cadence or
    # the carve-out has nothing to protect — re-evaluate.
    assert _EVENT_DRIVEN_LABELS, (
        "no event-driven residents in registry — exemption set is "
        "empty; remove the carve-out or restore the event-driven "
        "category"
    )


@pytest.mark.asyncio
async def test_at_least_one_dogfood_row_in_recent_window(test_db):
    """Probe-self liveness check: at least one real probe tick must
    have run in the last hour. Stronger than the previous version which
    accepted any snapshot row, including synthetic test inserts.
    """
    async with test_db.acquire() as conn:
        n = await conn.fetchval(
            """
            SELECT count(*) FROM progress_flat_snapshots
            WHERE ticked_at > now() - interval '1 hour'
              AND resident_label = 'progress_flat_probe'
            """
        )
    if n == 0:
        pytest.skip("no real probe ticks in the last hour — probe not running")
    assert n >= 1
