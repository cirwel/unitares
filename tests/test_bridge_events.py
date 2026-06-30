from datetime import datetime, timezone

import pytest

from src.bridge_events import (
    BridgeEventError,
    build_bridge_summary,
    normalize_bridge_event,
    record_bridge_event,
)


def test_normalize_delivery_receipt_defaults_status_and_surface():
    event = normalize_bridge_event(
        {
            "event_type": "bridge.delivery",
            "source_event_id": "evt-1",
            "source_event_type": "sentinel_finding",
            "source_severity": "critical",
            "channel_key": "alerts",
            "discord_message_id": "123",
        }
    )

    assert event["event_type"] == "bridge.delivery"
    assert event["kind"] == "delivery"
    assert event["status"] == "delivered"
    assert event["surface"] == "discord"
    assert event["severity"] == "critical"


def test_ack_requires_match_key():
    with pytest.raises(BridgeEventError, match="source_event_id or discord_message_id"):
        normalize_bridge_event({"event_type": "bridge.ack"})


@pytest.mark.asyncio
async def test_record_bridge_event_appends_audit_event(monkeypatch):
    calls = []

    async def fake_append(entry):
        calls.append(entry)
        return True

    monkeypatch.setattr("src.audit_db.append_audit_event_async", fake_append)

    result = await record_bridge_event(
        {
            "event_type": "bridge.command",
            "bridge_id": "discord-bridge",
            "command_name": "status",
            "operator_id_hash": "hash-only",
        }
    )

    assert result["success"] is True
    assert calls[0]["event_type"] == "bridge.command"
    assert calls[0]["agent_id"] == "discord-bridge"
    assert calls[0]["details"]["schema"] == "unitares.bridge_event.v1"


@pytest.mark.asyncio
async def test_bridge_summary_counts_unacked_critical_delivery(monkeypatch):
    now = datetime(2026, 6, 30, tzinfo=timezone.utc).isoformat()

    async def fake_query(**kwargs):
        assert "bridge.delivery" in kwargs["event_types"]
        return [
            {
                "timestamp": now,
                "event_id": "row-1",
                "agent_id": "discord-bridge",
                "event_type": "bridge.delivery",
                "details": {
                    "event_type": "bridge.delivery",
                    "kind": "delivery",
                    "status": "delivered",
                    "severity": "critical",
                    "source_event_id": "evt-critical",
                    "channel_key": "alerts",
                    "discord_message_id": "msg-1",
                },
            },
            {
                "timestamp": now,
                "event_id": "row-2",
                "agent_id": "discord-bridge",
                "event_type": "bridge.delivery_failed",
                "details": {
                    "event_type": "bridge.delivery_failed",
                    "kind": "delivery_failed",
                    "status": "failed",
                    "severity": "high",
                    "source_event_id": "evt-failed",
                    "channel_key": "alerts",
                    "error": "Forbidden",
                },
            },
        ]

    monkeypatch.setattr("src.audit_db.query_audit_events_async", fake_query)

    summary = await build_bridge_summary({"since": "24h", "limit": 20})

    assert summary["success"] is True
    assert summary["surface"] == "discord"
    assert summary["by_event_type"]["bridge.delivery"] == 1
    assert summary["by_event_type"]["bridge.delivery_failed"] == 1
    assert summary["unacked_critical_count"] == 1
    assert summary["unacked_critical"][0]["source_event_id"] == "evt-critical"
    assert summary["recent_failures"][0]["error"] == "Forbidden"


@pytest.mark.asyncio
async def test_bridge_summary_ack_matches_delivery(monkeypatch):
    now = datetime(2026, 6, 30, tzinfo=timezone.utc).isoformat()

    async def fake_query(**kwargs):
        return [
            {
                "timestamp": now,
                "event_id": "ack-row",
                "agent_id": "discord-bridge",
                "event_type": "bridge.ack",
                "details": {
                    "event_type": "bridge.ack",
                    "kind": "ack",
                    "status": "acked",
                    "severity": "critical",
                    "source_event_id": "evt-critical",
                    "discord_message_id": "msg-1",
                },
            },
            {
                "timestamp": now,
                "event_id": "delivery-row",
                "agent_id": "discord-bridge",
                "event_type": "bridge.delivery",
                "details": {
                    "event_type": "bridge.delivery",
                    "kind": "delivery",
                    "status": "delivered",
                    "severity": "critical",
                    "source_event_id": "evt-critical",
                    "discord_message_id": "msg-1",
                },
            },
        ]

    monkeypatch.setattr("src.audit_db.query_audit_events_async", fake_query)

    summary = await build_bridge_summary({"since": "24h"})

    assert summary["unacked_critical_count"] == 0
