from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
import uuid

import pytest

from scripts.eval import run_kg_agent_adoption_live_canary as v0
from scripts.eval import run_kg_agent_adoption_live_canary_v1 as canary


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _payloads(tasks: dict, discovery_id: str) -> dict:
    payloads = {}
    for chain in tasks["chains"]:
        for step in chain["steps"]:
            if not step["eligible_for_prior_work"]:
                continue
            payloads[step["step_id"]] = {
                "success": True,
                "search_mode_used": "fts",
                "search_mode_requested": "fts",
                "operator_used": "OR",
                "search_degraded": False,
                "fallback_used": False,
                "discoveries": [
                    {"id": discovery_id, "summary": discovery_id}
                ],
            }
    return payloads


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8767/mcp/",
        "http://localhost:8767/mcp",
        "http://[::1]:8767/mcp/",
    ],
)
def test_fresh_canary_requires_standalone_loopback_full_mcp(url: str) -> None:
    result = canary.validate_isolation_contract(
        mcp_url=url,
        probe_evidence_path=None,
        recover_event_id=None,
    )
    assert result["mode"] == "standalone_direct_loopback_mcp"
    assert result["host_slot_isolation_proven"] is True
    assert result["captured_plugin_probe_accepted_for_fresh_append"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8768/mcp/",
        "https://127.0.0.1:8767/mcp/",
        "http://example.com:8767/mcp/",
        "http://127.0.0.1:8767/",
    ],
)
def test_fresh_canary_rejects_nonisolated_or_wrong_transport(url: str) -> None:
    with pytest.raises(v0.LiveCanaryError, match="loopback"):
        canary.validate_isolation_contract(
            mcp_url=url,
            probe_evidence_path=None,
            recover_event_id=None,
        )


def test_captured_plugin_probe_is_recovery_only(tmp_path: Path) -> None:
    probe = tmp_path / "probe.json"
    with pytest.raises(v0.LiveCanaryError, match="read-only recovery"):
        canary.validate_isolation_contract(
            mcp_url="http://127.0.0.1:8767/mcp/",
            probe_evidence_path=probe,
            recover_event_id=None,
        )
    result = canary.validate_isolation_contract(
        mcp_url="http://127.0.0.1:8767/mcp/",
        probe_evidence_path=probe,
        recover_event_id=str(uuid.uuid4()),
    )
    assert result["fresh_audit_append_authorized"] is False
    assert result["host_slot_isolation_proven"] is False


def test_main_refuses_captured_plugin_probe_before_live_operations(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    mint = AsyncMock()
    append = AsyncMock()
    monkeypatch.setattr(v0, "_mint_canary_identity", mint)
    monkeypatch.setattr("src.audit_db.append_audit_event_async", append)

    result = canary.main(
        [
            "--authorize-live-canary",
            "--operator-authorization-ref",
            "test",
            "--probe-evidence",
            str(tmp_path / "captured.json"),
        ]
    )

    assert result == 1
    assert "read-only recovery" in capsys.readouterr().err
    mint.assert_not_awaited()
    append.assert_not_awaited()


def test_exact_audit_query_uses_normalized_application_api(monkeypatch) -> None:
    event_id = str(uuid.uuid4())
    query = AsyncMock(
        return_value=[
            {
                "timestamp": "2026-08-28T05:00:00+00:00",
                "event_id": event_id,
                "agent_id": None,
                "session_id": None,
                "event_type": v0.AUDIT_EVENT_TYPE,
                "confidence": 1.0,
                "details": {"nested": {"ok": True}},
                "raw_hash": "a" * 64,
            }
        ]
    )
    monkeypatch.setattr("src.audit_db.query_audit_events_async", query)

    [row] = asyncio.run(canary._query_exact_audit_event(event_id))

    assert row["payload"] == {"nested": {"ok": True}}
    query.assert_awaited_once_with(event_id=event_id, limit=2)


def test_audit_probe_appends_once_then_reads_canonical_row(monkeypatch) -> None:
    captured = {}

    async def append(entry, raw_hash=None):
        captured["entry"] = entry
        captured["raw_hash"] = raw_hash
        captured["calls"] = captured.get("calls", 0) + 1
        return True

    async def query(event_id):
        entry = captured["entry"]
        return [
            {
                "event_id": event_id,
                "event_type": entry["event_type"],
                "agent_id": None,
                "session_id": None,
                "payload": entry["details"],
                "raw_hash": captured["raw_hash"],
            }
        ]

    monkeypatch.setattr("src.audit_db.append_audit_event_async", append)
    monkeypatch.setattr(canary, "_query_exact_audit_event", query)

    result = asyncio.run(
        canary._run_audit_probe(
            experiment_id="experiment",
            master_commit="a" * 40,
            enrollment_sha256="b" * 64,
            task_manifest_sha256="c" * 64,
        )
    )

    assert captured["calls"] == 1
    assert result["immediate_readback_exact"] is True
    assert result["recovery_used"] is False


def test_full_v1_receipt_records_isolation_without_promoting(monkeypatch) -> None:
    tasks = _load(v0.DEFAULT_TASKS)
    source_map = _load(v0.DEFAULT_SOURCE_MAP)
    identity = {
        "agent_uuid": "4d54b929-ab24-49b3-a609-2bdd0ab86e35",
        "client_session_id": "agent-4d54b929-ab2",
        "label": "canary_agent_adoption",
    }
    monkeypatch.setattr(v0, "_mint_canary_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(
        v0,
        "_fetch_discovery",
        AsyncMock(return_value=({"id": source_map["discovery"]["id"]}, 2)),
    )
    monkeypatch.setattr(
        v0,
        "validate_source_map",
        lambda *_: {
            "record_sha256": source_map["discovery"]["record_sha256"],
            "mapping_sha256": source_map["mapping_sha256"],
            "relation": "derived_projection",
            "byte_equivalent": False,
        },
    )
    monkeypatch.setattr(
        v0,
        "_run_live_searches",
        AsyncMock(
            return_value=_payloads(tasks, source_map["discovery"]["id"])
        ),
    )
    monkeypatch.setattr(
        v0,
        "_await_tool_usage_attribution",
        AsyncMock(return_value={"all_rows_attributed": True}),
    )
    monkeypatch.setattr(
        v0,
        "_validate_calibration_exclusion",
        AsyncMock(
            return_value={
                "measured_state_rows": 0,
                "outcome_rows": 0,
                "adoption_event_rows": 0,
                "point_in_time_zero": True,
                "durable_exclusion_proven": False,
            }
        ),
    )
    monkeypatch.setattr(
        canary,
        "_run_audit_probe",
        AsyncMock(
            return_value={
                "event_type": v0.AUDIT_EVENT_TYPE,
                "append_attempts": 1,
                "exact_readback": True,
                "immediate_readback_exact": True,
                "recovery_used": False,
            }
        ),
    )
    close_db = AsyncMock()
    monkeypatch.setattr("src.db.close_db", close_db)

    receipt = asyncio.run(
        canary.run_live_canary(
            enrollment_path=v0.DEFAULT_ENROLLMENT,
            tasks_path=v0.DEFAULT_TASKS,
            source_map_path=v0.DEFAULT_SOURCE_MAP,
            offline_receipt_path=v0.DEFAULT_OFFLINE_RECEIPT,
            mcp_url="http://127.0.0.1:8767/mcp/",
            timeout_s=1,
            authorization_ref="operator-test",
        )
    )

    content = receipt["content"]
    assert content["schema"] == canary.LIVE_RECEIPT_SCHEMA
    assert content["status"] == "hold"
    assert content["claims"]["host_slot_isolation_proven"] is True
    assert content["claims"]["durable_calibration_exclusion_proven"] is False
    assert content["claims"]["production_plumbing_fully_proven"] is False
    assert content["attempted_operations"]["audit_append_attempts_this_run"] == 1
    close_db.assert_awaited_once()
