from __future__ import annotations

from datetime import datetime, timezone

from src.identity.agent_fragmentation import build_agent_fragmentation_snapshot


NOW = datetime(2026, 5, 24, tzinfo=timezone.utc)


def _row(**overrides):
    row = {
        "identity_id": 1,
        "agent_id": "agent-a",
        "status": "active",
        "created_at": datetime(2026, 5, 20, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 20, tzinfo=timezone.utc),
        "last_activity_at": None,
        "parent_agent_id": None,
        "spawn_reason": "new_session",
        "provisional_lineage": False,
        "chain_obs_count": 0,
        "label": "",
        "model_type": "claude",
        "purpose": "",
        "thread_id": "thread-a",
        "active_session_key": None,
        "metadata_total_updates": 0,
        "real_checkins": 0,
        "bootstrap_rows": 0,
        "first_real_checkin_at": None,
        "last_real_checkin_at": None,
        "last_any_state_at": None,
        "session_resolution_source": None,
        "transport": None,
        "context_source": None,
    }
    row.update(overrides)
    return row


def _synthetic_rows(*, measured=10, synthetic=0):
    return [
        {"synthetic": False, "rows": measured, "identities": 3},
        {"synthetic": True, "rows": synthetic, "identities": synthetic},
    ]


def test_snapshot_flags_stale_active_zero_real_identities():
    report = build_agent_fragmentation_snapshot(
        [_row()],
        _synthetic_rows(measured=10, synthetic=1),
        observed_at=NOW,
        stale_hours=24,
    )

    assert report["decision"] == "attention"
    assert report["reason"] == "active_identities_without_measured_trajectory"
    assert report["totals"]["active_zero_real_checkins"] == 1
    assert report["totals"]["active_zero_real_stale"] == 1
    assert report["totals"]["no_state_at_all"] == 1
    assert report["totals"]["bootstrap_only"] == 0
    assert "initial_state" in report["recommendations"][0]


def test_snapshot_distinguishes_bootstrap_only_from_no_state():
    report = build_agent_fragmentation_snapshot(
        [_row(bootstrap_rows=1)],
        _synthetic_rows(measured=10, synthetic=1),
        observed_at=NOW,
    )

    assert report["totals"]["no_state_at_all"] == 0
    assert report["totals"]["bootstrap_only"] == 1


def test_snapshot_reports_bootstrap_absence_even_without_stale_active_agents():
    report = build_agent_fragmentation_snapshot(
        [
            _row(
                status="archived",
                created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
            )
        ],
        _synthetic_rows(measured=10, synthetic=0),
        observed_at=NOW,
    )

    assert report["decision"] == "watch"
    assert report["reason"] == "bootstrap_rows_absent_or_nearly_absent"
    assert report["state_rows"]["synthetic_rows"] == 0


def test_thread_clusters_group_active_low_checkin_agents():
    report = build_agent_fragmentation_snapshot(
        [
            _row(agent_id="agent-a", label="Agent A", real_checkins=0),
            _row(agent_id="agent-b", label="Agent B", real_checkins=2),
            _row(agent_id="agent-c", label="Agent C", real_checkins=9),
            _row(agent_id="agent-d", thread_id="solo", real_checkins=1),
        ],
        _synthetic_rows(measured=20, synthetic=5),
        observed_at=NOW,
        low_checkin_max=3,
    )

    assert report["thread_clusters"] == [
        {
            "thread_id": "thread-a",
            "active_low_identities": 2,
            "zero_real_checkins": 1,
            "one_to_low_real_checkins": 1,
            "first_created_at": "2026-05-20T00:00:00+00:00",
            "last_created_at": "2026-05-20T00:00:00+00:00",
            "sample_labels": ["Agent A", "Agent B"],
        }
    ]


def test_recent_session_source_grouping_counts_zero_and_sparse():
    report = build_agent_fragmentation_snapshot(
        [
            _row(
                created_at=datetime(2026, 5, 23, 12, tzinfo=timezone.utc),
                session_resolution_source="explicit_client_session_id",
                transport="mcp",
                real_checkins=0,
            ),
            _row(
                created_at=datetime(2026, 5, 23, 11, tzinfo=timezone.utc),
                session_resolution_source="explicit_client_session_id",
                transport="mcp",
                real_checkins=3,
            ),
            _row(
                created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
                session_resolution_source="old",
                transport="rest",
                real_checkins=0,
            ),
        ],
        _synthetic_rows(measured=20, synthetic=5),
        observed_at=NOW,
    )

    assert report["recent"]["created_7d"]["identities"] == 2
    assert report["recent_7d_by_session_source"] == [
        {
            "session_resolution_source": "explicit_client_session_id",
            "transport": "mcp",
            "identities": 2,
            "zero_real_checkins": 1,
            "one_real_checkin": 0,
            "one_to_low_real_checkins": 1,
            "more_than_low_real_checkins": 0,
        }
    ]
