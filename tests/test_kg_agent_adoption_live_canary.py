from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.eval.kg_agent_adoption import sha256_json
from scripts.eval import run_kg_agent_adoption_live_canary as canary


ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "tests/kg_agent_adoption/task-chains-v0.json"
SOURCE_MAP_PATH = (
    ROOT / "docs/evaluations/kg-agent-adoption/live-source-map-v0.json"
)
PROBE_PATH = ROOT / "docs/evaluations/kg-agent-adoption/live-mcp-probe-v0.json"
LIVE_RECEIPT_PATH = (
    ROOT
    / "docs/evaluations/kg-agent-adoption/live-plumbing-canary-v0.receipt.json"
)
ATTESTATION_PATH = (
    ROOT / "docs/evaluations/kg-agent-adoption/live-canary-attestation-v0.json"
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _synthetic_discovery_and_map() -> tuple[dict, dict]:
    source_map = _load(SOURCE_MAP_PATH)
    summary = next(
        item["support_fragment"]
        for item in source_map["logical_sources"]
        if item["support_field"] == "summary"
    )
    details = "\n".join(
        item["support_fragment"]
        for item in source_map["logical_sources"]
        if item["support_field"] == "details"
    )
    discovery = {
        "id": source_map["discovery"]["id"],
        "type": "architectural_decision",
        "status": "open",
        "summary": summary,
        "details": details,
        "tags": ["projection-test", "agent-adoption"],
    }
    source_map["discovery"]["record_sha256"] = sha256_json(
        canary.canonical_live_projection(discovery)
    )
    return discovery, source_map


def _search_payload(ids: list[str], *, fallback: bool = False) -> dict:
    payload = {
        "success": True,
        "search_mode_used": "fts",
        "search_mode_requested": "fts",
        "operator_used": "OR",
        "discoveries": [{"id": value, "summary": value} for value in ids],
    }
    if fallback:
        payload["fallback_used"] = True
    return payload


def _payloads(tasks: dict, discovery_id: str, *, miss_first: bool = False) -> dict:
    payloads = {}
    for chain in tasks["chains"]:
        for step in chain["steps"]:
            if step["eligible_for_prior_work"]:
                ids = [] if miss_first and not payloads else [discovery_id]
                payloads[step["step_id"]] = _search_payload(ids)
    return payloads


def test_reviewed_offline_basis_preserves_original_receipt_digest() -> None:
    enrollment_path = canary.DEFAULT_ENROLLMENT
    tasks_path = canary.DEFAULT_TASKS
    reviewed = canary.validate_reviewed_offline_basis(
        _load(enrollment_path),
        enrollment_path=enrollment_path,
        tasks=_load(tasks_path),
        tasks_path=tasks_path,
        offline_receipt=_load(canary.DEFAULT_OFFLINE_RECEIPT),
    )
    assert reviewed["offline_receipt_content_sha256"] == (
        "b009eeddcc732be5cd463cef913c2cee218463ed3fb81d055db02d714201f9c2"
    )
    assert reviewed["frozen_digests"]["enrollment_sha256"] == (
        "872e28cc35f8d7f280a7b4523b506a171cc8f622775b864e77b8965f90992387"
    )


def test_live_receipt_and_later_attestation_are_content_addressed() -> None:
    receipt = _load(LIVE_RECEIPT_PATH)
    attestation = _load(ATTESTATION_PATH)
    assert sha256_json(receipt["content"]) == receipt["receipt_content_sha256"]
    assert receipt["content"]["current_digests"]["live_source_map_sha256"] == (
        canary.sha256_file(SOURCE_MAP_PATH)
    )
    assert receipt["content"]["current_digests"]["live_mcp_probe_sha256"] == (
        canary.sha256_file(PROBE_PATH)
    )
    assert receipt["content"]["claims"]["audit_recording_path_proven"] is False
    assert sha256_json(attestation["content"]) == attestation[
        "receipt_content_sha256"
    ]
    artifacts = attestation["content"]["artifacts"]
    assert artifacts["live_runner_sha256"] == canary.sha256_file(Path(canary.__file__))
    assert artifacts["live_receipt_file_sha256"] == canary.sha256_file(
        LIVE_RECEIPT_PATH
    )
    assert artifacts["live_receipt_content_sha256"] == receipt[
        "receipt_content_sha256"
    ]
    assert attestation["content"]["claims"]["durable_canary_isolation_proven"] is False


def test_source_map_is_derived_and_never_byte_equivalent() -> None:
    tasks = _load(TASKS_PATH)
    discovery, source_map = _synthetic_discovery_and_map()
    result = canary.validate_source_map(tasks, source_map, discovery)
    assert result["relation"] == "derived_projection"
    assert result["byte_equivalent"] is False
    assert len(result["logical_source_ids"]) == 4


def test_measurement_exclusion_contract_matches_live_kpi_queries() -> None:
    result = canary.validate_measurement_exclusion_contract(
        "canary_agent_adoption"
    )
    assert result["excluded_from_adoption_kpis"] is True
    with pytest.raises(canary.LiveCanaryError, match="not excluded"):
        canary.validate_measurement_exclusion_contract("ordinary-agent")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda source_map, discovery: discovery.update(details="drift"),
            "live KG root drifted",
        ),
        (
            lambda source_map, discovery: source_map.update(byte_equivalent=True),
            "byte_equivalent=false",
        ),
        (
            lambda source_map, discovery: source_map["logical_sources"][0].update(
                byte_equivalent=True
            ),
            "false equivalence",
        ),
    ],
)
def test_source_map_fails_closed_on_drift(mutation, message: str) -> None:
    tasks = _load(TASKS_PATH)
    discovery, source_map = _synthetic_discovery_and_map()
    mutation(source_map, discovery)
    with pytest.raises(canary.LiveCanaryError, match=message):
        canary.validate_source_map(tasks, source_map, discovery)


def test_search_summary_records_top_five_miss_without_inventing_parity() -> None:
    tasks = _load(TASKS_PATH)
    source_map = _load(SOURCE_MAP_PATH)
    discovery_id = source_map["discovery"]["id"]
    checks = canary.summarize_search_results(
        tasks,
        source_map,
        _payloads(tasks, discovery_id, miss_first=True),
    )
    assert checks[0]["passed"] is False
    assert next(iter(checks[0]["canonical_ranks"].values())) is None
    assert all(check["passed"] for check in checks[1:])


def test_search_summary_rejects_fallback_as_parity_evidence() -> None:
    tasks = _load(TASKS_PATH)
    source_map = _load(SOURCE_MAP_PATH)
    payloads = _payloads(tasks, source_map["discovery"]["id"])
    first = next(iter(payloads))
    payloads[first]["fallback_used"] = True
    with pytest.raises(canary.LiveCanaryError, match="degraded/fallback"):
        canary.summarize_search_results(tasks, source_map, payloads)


def test_audit_readback_requires_one_neutral_exact_row() -> None:
    event_id = "811e3f89-48cd-4fd0-aed5-c74e1b2dc841"
    content = {"schema": canary.AUDIT_CONTENT_SCHEMA, "canary_id": event_id}
    digest = sha256_json(content)
    details = {"content": content, "content_sha256": digest}
    row = {
        "event_id": event_id,
        "event_type": canary.AUDIT_EVENT_TYPE,
        "agent_id": None,
        "session_id": None,
        "payload": details,
        "raw_hash": digest,
    }
    result = canary.validate_audit_readback(
        event_id=event_id,
        expected_details=details,
        rows=[row],
    )
    assert result["append_attempts"] == 1
    assert result["exact_readback"] is True
    encoded = deepcopy(row)
    encoded["payload"] = json.dumps(details)
    assert canary.validate_audit_readback(
        event_id=event_id,
        expected_details=details,
        rows=[encoded],
    )["exact_readback"] is True
    with pytest.raises(canary.LiveCanaryError, match="expected one audit row"):
        canary.validate_audit_readback(
            event_id=event_id,
            expected_details=details,
            rows=[row, deepcopy(row)],
        )


def test_tool_usage_attribution_requires_canary_label_and_session() -> None:
    identity = {
        "agent_uuid": "4d54b929-ab24-49b3-a609-2bdd0ab86e35",
        "client_session_id": "agent-4d54b929-ab2",
        "label": "canary_agent_adoption",
    }
    rows = []
    for action in ["details", "details", *(["search"] * 6)]:
        rows.append(
            {
                "agent_id": identity["agent_uuid"],
                "session_id": identity["client_session_id"],
                "tool_name": "knowledge",
                "payload": {"action": action},
                "label": identity["label"],
            }
        )
    result = canary.validate_tool_usage_attribution(
        identity=identity,
        rows=rows,
        expected_searches=6,
        expected_details=2,
    )
    assert result["all_rows_attributed"] is True
    encoded_rows = deepcopy(rows)
    for row in encoded_rows:
        row["payload"] = json.dumps(row["payload"])
    assert canary.validate_tool_usage_attribution(
        identity=identity,
        rows=encoded_rows,
        expected_searches=6,
        expected_details=2,
    )["all_rows_attributed"] is True
    rows[0]["label"] = "ordinary-agent"
    with pytest.raises(canary.LiveCanaryError, match="missing canary-labelled"):
        canary.validate_tool_usage_attribution(
            identity=identity,
            rows=rows,
            expected_searches=6,
            expected_details=2,
        )
    rows[0]["label"] = identity["label"]
    rows.append(deepcopy(rows[-1]))
    with pytest.raises(canary.LiveCanaryError, match="expected 6 recorded KG searches"):
        canary.validate_tool_usage_attribution(
            identity=identity,
            rows=rows,
            expected_searches=6,
            expected_details=2,
        )


def test_audit_probe_appends_once_and_reads_exact_row(monkeypatch) -> None:
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
                "agent_id": entry["agent_id"],
                "session_id": entry["session_id"],
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
    assert captured["entry"]["event_type"] == canary.AUDIT_EVENT_TYPE
    assert captured["entry"]["agent_id"] is None
    assert captured["entry"]["session_id"] is None
    assert result["matching_rows"] == 1


def test_audit_recovery_is_read_only_and_preserves_failed_immediate_status(
    monkeypatch,
) -> None:
    event_id = "811e3f89-48cd-4fd0-aed5-c74e1b2dc841"
    content = {
        "schema": canary.AUDIT_CONTENT_SCHEMA,
        "canary_id": event_id,
        "purpose": "agent_adoption_recording_gate",
        "experiment_id": "experiment",
        "master_commit": "a" * 40,
        "enrollment_sha256": "b" * 64,
        "task_manifest_sha256": "c" * 64,
        "created_at": "2026-08-28T04:45:01+00:00",
        "count_toward_adoption": False,
        "count_toward_calibration": False,
    }
    digest = sha256_json(content)
    details = {"content": content, "content_sha256": digest}
    query = AsyncMock(
        return_value=[
            {
                "event_id": event_id,
                "event_type": canary.AUDIT_EVENT_TYPE,
                "agent_id": None,
                "session_id": None,
                "payload": json.dumps(details),
                "raw_hash": digest,
            }
        ]
    )
    monkeypatch.setattr(canary, "_query_exact_audit_event", query)
    result = asyncio.run(
        canary._recover_audit_probe(
            event_id=event_id,
            experiment_id="experiment",
            master_commit="a" * 40,
            enrollment_sha256="b" * 64,
            task_manifest_sha256="c" * 64,
        )
    )
    assert result["immediate_readback_exact"] is False
    assert result["recovery_exact_readback"] is True
    assert result["recovery_used"] is True
    query.assert_awaited_once_with(event_id)


def test_main_refuses_before_any_live_operation(monkeypatch, capsys) -> None:
    mint = AsyncMock()
    monkeypatch.setattr(canary, "_mint_canary_identity", mint)
    result = canary.main(["--operator-authorization-ref", "test"])
    assert result == 2
    assert "no live operations attempted" in capsys.readouterr().err
    mint.assert_not_called()


def test_recovery_requires_bound_probe_evidence(monkeypatch, capsys) -> None:
    mint = AsyncMock()
    monkeypatch.setattr(canary, "_mint_canary_identity", mint)
    result = canary.main(
        [
            "--authorize-live-canary",
            "--operator-authorization-ref",
            "test",
            "--recover-event-id",
            "811e3f89-48cd-4fd0-aed5-c74e1b2dc841",
        ]
    )
    assert result == 1
    assert "requires the bound MCP probe evidence" in capsys.readouterr().err
    mint.assert_not_called()


def test_full_canary_receipt_stays_hold_with_mocked_live_seams(monkeypatch) -> None:
    tasks = _load(canary.DEFAULT_TASKS)
    source_map = _load(canary.DEFAULT_SOURCE_MAP)
    identity = {
        "agent_uuid": "4d54b929-ab24-49b3-a609-2bdd0ab86e35",
        "client_session_id": "agent-4d54b929-ab2",
        "label": "canary_agent_adoption",
    }
    monkeypatch.setattr(canary, "_mint_canary_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(
        canary,
        "_fetch_discovery",
        AsyncMock(return_value=({"id": source_map["discovery"]["id"]}, 2)),
    )
    monkeypatch.setattr(
        canary,
        "validate_source_map",
        lambda *_: {
            "record_sha256": source_map["discovery"]["record_sha256"],
            "mapping_sha256": source_map["mapping_sha256"],
            "relation": "derived_projection",
            "byte_equivalent": False,
        },
    )
    monkeypatch.setattr(
        canary,
        "_run_live_searches",
        AsyncMock(
            return_value=_payloads(
                tasks,
                source_map["discovery"]["id"],
                miss_first=True,
            )
        ),
    )
    monkeypatch.setattr(
        canary,
        "_await_tool_usage_attribution",
        AsyncMock(return_value={"all_rows_attributed": True}),
    )
    monkeypatch.setattr(
        canary,
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
                "event_type": canary.AUDIT_EVENT_TYPE,
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
            enrollment_path=canary.DEFAULT_ENROLLMENT,
            tasks_path=canary.DEFAULT_TASKS,
            source_map_path=canary.DEFAULT_SOURCE_MAP,
            offline_receipt_path=canary.DEFAULT_OFFLINE_RECEIPT,
            mcp_url="http://example.invalid/mcp/",
            timeout_s=1,
            authorization_ref="operator-test",
        )
    )
    content = receipt["content"]
    assert content["status"] == "hold"
    assert content["claims"]["canonical_root_top_k_for_all_queries"] is False
    assert content["claims"]["logical_source_parity"] is False
    assert content["claims"]["audit_recording_path_proven"] is True
    assert content["attempted_operations"]["audit_append_attempts"] == 1
    close_db.assert_awaited_once()
