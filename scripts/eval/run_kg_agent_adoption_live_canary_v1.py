#!/usr/bin/env python3
"""Run the isolated UNITARES adoption infrastructure canary v1.

Version 0 is immutable evidence for the 2026-08-27 production-plugin probe.
This follow-up preserves its frozen retrieval contract while closing two seams:

* a fresh canary may run only through the full loopback MCP endpoint from this
  standalone process; captured in-slot plugin evidence is recovery-only;
* audit readback uses the canonical audit query API, which returns normalized
  JSON payloads and supports exact event-ID filtering.

The runner remains HOLD-only. It does not call a model, write the KG, record an
outcome, execute a scored task, invoke orchestration, or surface an alert.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
import uuid

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval import run_kg_agent_adoption_live_canary as v0  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIVE_RECEIPT = (
    REPO_ROOT
    / "docs/evaluations/kg-agent-adoption/live-plumbing-canary-v1.receipt.json"
)
LIVE_RECEIPT_SCHEMA = "unitares.kg-agent-adoption.live-canary.v1"
ISOLATION_CONTRACT_SCHEMA = "unitares.kg-agent-adoption.isolation-contract.v1"
FULL_MCP_PORT = 8767


def validate_isolation_contract(
    *,
    mcp_url: str,
    probe_evidence_path: Path | None,
    recover_event_id: str | None,
) -> dict[str, Any]:
    """Refuse any fresh append whose probe reused an interactive host slot."""
    if probe_evidence_path is not None:
        v0._require(
            recover_event_id is not None,
            "captured plugin evidence is read-only recovery evidence and may "
            "not authorize a fresh audit append",
        )
        return {
            "schema": ISOLATION_CONTRACT_SCHEMA,
            "mode": "captured_plugin_read_only_recovery",
            "fresh_probe_transport": False,
            "fresh_audit_append_authorized": False,
            "host_slot_isolation_proven": False,
        }

    v0._require(
        recover_event_id is None,
        "read-only recovery requires the bound captured probe evidence",
    )
    parsed = urlsplit(mcp_url)
    v0._require(
        parsed.scheme == "http"
        and parsed.hostname is not None
        and parsed.port == FULL_MCP_PORT
        and parsed.path.rstrip("/") == "/mcp"
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment,
        "fresh canary requires the direct loopback full-MCP endpoint "
        "http://127.0.0.1:8767/mcp/",
    )
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        v0._require(
            parsed.hostname == "localhost",
            "fresh canary MCP host must be loopback",
        )
    else:
        v0._require(address.is_loopback, "fresh canary MCP host must be loopback")
    return {
        "schema": ISOLATION_CONTRACT_SCHEMA,
        "mode": "standalone_direct_loopback_mcp",
        "fresh_probe_transport": True,
        "fresh_audit_append_authorized": True,
        "host_slot_isolation_proven": True,
        "endpoint": f"http://{parsed.netloc}/mcp/",
        "full_mcp_port": FULL_MCP_PORT,
        "captured_plugin_probe_accepted_for_fresh_append": False,
    }


async def _query_exact_audit_event(event_id: str) -> list[dict[str, Any]]:
    """Read one event through the normalized application query seam."""
    from src.audit_db import query_audit_events_async

    rows = await query_audit_events_async(event_id=event_id, limit=2)
    return [
        {
            "timestamp": row["timestamp"],
            "event_id": row["event_id"],
            "agent_id": row["agent_id"],
            "session_id": row["session_id"],
            "event_type": row["event_type"],
            "confidence": row["confidence"],
            "payload": row["details"],
            "raw_hash": row["raw_hash"],
        }
        for row in rows
    ]


async def _run_audit_probe(
    *,
    experiment_id: str,
    master_commit: str,
    enrollment_sha256: str,
    task_manifest_sha256: str,
) -> dict[str, Any]:
    from src.audit_db import append_audit_event_async

    event_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)
    content = {
        "schema": v0.AUDIT_CONTENT_SCHEMA,
        "canary_id": event_id,
        "purpose": "agent_adoption_recording_gate",
        "experiment_id": experiment_id,
        "master_commit": master_commit,
        "enrollment_sha256": enrollment_sha256,
        "task_manifest_sha256": task_manifest_sha256,
        "created_at": created_at.isoformat(),
        "count_toward_adoption": False,
        "count_toward_calibration": False,
    }
    content_digest = v0.sha256_json(content)
    details = {"content": content, "content_sha256": content_digest}
    entry = {
        "timestamp": created_at.isoformat(),
        "event_id": event_id,
        "event_type": v0.AUDIT_EVENT_TYPE,
        "agent_id": None,
        "session_id": None,
        "confidence": 1.0,
        "details": details,
    }
    appended = await append_audit_event_async(entry, raw_hash=content_digest)
    v0._require(appended is True, "awaited audit append returned false")
    rows = await _query_exact_audit_event(event_id)
    return v0.validate_audit_readback(
        event_id=event_id,
        expected_details=details,
        rows=rows,
    )


async def _recover_audit_probe(
    *,
    event_id: str,
    experiment_id: str,
    master_commit: str,
    enrollment_sha256: str,
    task_manifest_sha256: str,
) -> dict[str, Any]:
    """Recover the one historical row without another append."""
    rows = await _query_exact_audit_event(event_id)
    v0._require(len(rows) == 1, f"expected one recovery row for {event_id}")
    payload = rows[0].get("payload")
    v0._require(isinstance(payload, Mapping), "recovery payload is not an object")
    content = payload.get("content")
    v0._require(isinstance(content, Mapping), "recovery content is missing")
    created_at = content.get("created_at")
    v0._require(isinstance(created_at, str), "recovery created_at is missing")
    parsed_created_at = datetime.fromisoformat(created_at)
    v0._require(
        parsed_created_at.tzinfo is not None,
        "recovery created_at is offset-naive",
    )
    expected_content = {
        "schema": v0.AUDIT_CONTENT_SCHEMA,
        "canary_id": event_id,
        "purpose": "agent_adoption_recording_gate",
        "experiment_id": experiment_id,
        "master_commit": master_commit,
        "enrollment_sha256": enrollment_sha256,
        "task_manifest_sha256": task_manifest_sha256,
        "created_at": created_at,
        "count_toward_adoption": False,
        "count_toward_calibration": False,
    }
    v0._require(
        content == expected_content,
        "recovery content differs from the closed schema",
    )
    readback = v0.validate_audit_readback(
        event_id=event_id,
        expected_details=payload,
        rows=rows,
    )
    return {
        **readback,
        "append_attempts": 1,
        "append_attempts_this_run": 0,
        "immediate_readback_exact": False,
        "recovery_used": True,
        "recovery_exact_readback": True,
        "recovery_reason": "historical row verified through canonical JSON query",
    }


async def run_live_canary(
    *,
    enrollment_path: Path,
    tasks_path: Path,
    source_map_path: Path,
    offline_receipt_path: Path,
    mcp_url: str,
    timeout_s: float,
    authorization_ref: str,
    probe_evidence_path: Path | None = None,
    recover_event_id: str | None = None,
) -> dict[str, Any]:
    """Run the v1 isolation contract while reusing the frozen v0 validators."""
    from src.db import close_db

    isolation = validate_isolation_contract(
        mcp_url=mcp_url,
        probe_evidence_path=probe_evidence_path,
        recover_event_id=recover_event_id,
    )
    enrollment = v0._load_json(enrollment_path)
    tasks = v0._load_json(tasks_path)
    source_map = v0._load_json(source_map_path)
    offline_receipt = v0._load_json(offline_receipt_path)
    reviewed = v0.validate_reviewed_offline_basis(
        enrollment,
        enrollment_path=enrollment_path,
        tasks=tasks,
        tasks_path=tasks_path,
        offline_receipt=offline_receipt,
    )
    probe_evidence = (
        v0._load_json(probe_evidence_path)
        if probe_evidence_path is not None
        else None
    )
    if probe_evidence is not None:
        v0._require(
            probe_evidence.get("schema") == v0.PROBE_EVIDENCE_SCHEMA,
            "live MCP probe evidence schema mismatch",
        )
        v0._require(
            probe_evidence.get("transport") == "codex-plugin-mcp",
            "probe evidence did not use the production UNITARES plugin transport",
        )
        started_at = datetime.fromisoformat(probe_evidence["started_at"])
        v0._require(
            started_at.tzinfo is not None,
            "probe evidence started_at must include a UTC offset",
        )
    else:
        started_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    try:
        if probe_evidence is not None:
            identity = dict(probe_evidence["identity"])
            details_pages = int(probe_evidence["details_pages"])
            v0._require(details_pages > 0, "probe evidence has no KG details reads")
            discovery = probe_evidence["discovery"]
            search_payloads = probe_evidence["search_payloads"]
            v0._require(
                isinstance(search_payloads, Mapping),
                "probe evidence search payloads are missing",
            )
        else:
            identity = await v0._mint_canary_identity(mcp_url, timeout_s)
        exclusion_contract = v0.validate_measurement_exclusion_contract(
            identity["label"]
        )
        if probe_evidence is None:
            discovery, details_pages = await v0._fetch_discovery(
                mcp_url,
                source_map["discovery"]["id"],
                client_session_id=identity["client_session_id"],
                timeout_s=timeout_s,
            )
        projection = v0.validate_source_map(tasks, source_map, discovery)
        if probe_evidence is None:
            search_payloads = await v0._run_live_searches(
                mcp_url,
                tasks,
                client_session_id=identity["client_session_id"],
                timeout_s=timeout_s,
            )
        search_checks = v0.summarize_search_results(
            tasks,
            source_map,
            search_payloads,
        )
        attribution = await v0._await_tool_usage_attribution(
            identity=identity,
            started_at=started_at,
            expected_searches=len(search_checks),
            expected_details=details_pages,
        )
        calibration_exclusion = await v0._validate_calibration_exclusion(
            identity=identity,
            started_at=started_at,
        )
        enrollment_digest = v0.sha256_file(enrollment_path)
        task_digest = v0.sha256_file(tasks_path)
        if recover_event_id is not None:
            audit = await _recover_audit_probe(
                event_id=recover_event_id,
                experiment_id=enrollment["experiment_id"],
                master_commit=source_map["code"]["master_commit"],
                enrollment_sha256=enrollment_digest,
                task_manifest_sha256=task_digest,
            )
        else:
            audit = await _run_audit_probe(
                experiment_id=enrollment["experiment_id"],
                master_commit=source_map["code"]["master_commit"],
                enrollment_sha256=enrollment_digest,
                task_manifest_sha256=task_digest,
            )
    finally:
        await close_db()

    all_queries_top_k = all(check["passed"] for check in search_checks)
    content = {
        "schema": LIVE_RECEIPT_SCHEMA,
        "scope": "production_plumbing_only",
        "status": "hold",
        "experiment_id": enrollment["experiment_id"],
        "operator_authorization_ref": authorization_ref,
        "code": dict(source_map["code"]),
        "isolation_contract": isolation,
        "review_basis": {
            "scope": enrollment["review"]["scope"],
            "governed_review_session": reviewed["review_session"],
            "approved_by": reviewed["approved_by"],
            "offline_receipt_content_sha256": reviewed[
                "offline_receipt_content_sha256"
            ],
        },
        "frozen": {
            key: reviewed["frozen_digests"][key]
            for key in (
                "task_manifest_sha256",
                "enrollment_sha256",
                "substitute_corpus_sha256",
                "schedule_sha256",
            )
        },
        "current_digests": {
            "promoted_enrollment_sha256": enrollment_digest,
            "task_manifest_sha256": task_digest,
            "live_source_map_sha256": v0.sha256_file(source_map_path),
            **(
                {"live_mcp_probe_sha256": v0.sha256_file(probe_evidence_path)}
                if probe_evidence_path is not None
                else {}
            ),
        },
        "probe_transport": (
            probe_evidence["transport"]
            if probe_evidence is not None
            else "standalone-direct-loopback-mcp"
        ),
        "live_snapshot": {
            "discovery_id": source_map["discovery"]["id"],
            "projection": v0.LIVE_PROJECTION_SCHEMA,
            "record_sha256": projection["record_sha256"],
        },
        "logical_mapping": {
            "relation": projection["relation"],
            "byte_equivalent": projection["byte_equivalent"],
            "mapping_sha256": projection["mapping_sha256"],
        },
        "retrieval_contract": {
            **v0.RETRIEVAL_CONTRACT,
            "contract_sha256": v0.sha256_json(v0.RETRIEVAL_CONTRACT),
        },
        "probes": search_checks,
        "canary_attribution": attribution,
        "measurement_exclusion": {
            "contract": exclusion_contract,
            "postflight": calibration_exclusion,
        },
        "audit_canary": audit,
        "attempted_operations": {
            "live_model_calls": 0,
            "kg_reads": details_pages + len(search_checks),
            "kg_writes": 0,
            "audit_append_attempts_this_run": 0 if recover_event_id else 1,
            "bound_audit_append_attempts": 1,
            "distinct_audit_rows": 1,
            "outcome_writes": 0,
            "scored_task_steps": 0,
            "orchestration_calls": 0,
        },
        "claims": {
            "offline_fixture_reviewed": True,
            "canonical_root_details_reachable": True,
            "canonical_root_top_k_for_all_queries": all_queries_top_k,
            "logical_source_parity": False,
            "raw_live_corpus_byte_identity": False,
            "host_slot_isolation_proven": isolation[
                "host_slot_isolation_proven"
            ],
            "durable_calibration_exclusion_proven": False,
            "audit_recording_path_proven": audit["immediate_readback_exact"],
            "audit_row_persisted_and_exactly_recovered": audit["exact_readback"],
            "production_plumbing_fully_proven": False,
            "behavioral_evidence": False,
            "scored_run_authorized": False,
            "live_model_execution_authorized": False,
            "orchestration_adoption_gate_passed": False,
        },
        "hold_reasons": [
            "four frozen logical sources are derived projections of one live root",
            (
                "the live root is not top-five for every frozen query"
                if not all_queries_top_k
                else "derived projection is not four-source corpus parity"
            ),
            "no independent frozen behavioral task/corpus run has executed",
            "durable calibration exclusion still requires a quiet-period read",
            "orchestration reliability gates remain incomplete",
        ],
    }
    return {
        "schema": "unitares.content-addressed-receipt.v0",
        "receipt_content_sha256": v0.sha256_json(content),
        "content": content,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enrollment", type=Path, default=v0.DEFAULT_ENROLLMENT)
    parser.add_argument("--tasks", type=Path, default=v0.DEFAULT_TASKS)
    parser.add_argument("--source-map", type=Path, default=v0.DEFAULT_SOURCE_MAP)
    parser.add_argument(
        "--offline-receipt",
        type=Path,
        default=v0.DEFAULT_OFFLINE_RECEIPT,
    )
    parser.add_argument("--receipt", type=Path, default=DEFAULT_LIVE_RECEIPT)
    parser.add_argument(
        "--mcp-url",
        default=os.environ.get("UNITARES_MCP_URL", "http://127.0.0.1:8767/mcp/"),
    )
    parser.add_argument("--timeout-s", type=float, default=30.0)
    parser.add_argument(
        "--probe-evidence",
        type=Path,
        default=None,
        help="Historical production-plugin probe; accepted only for recovery",
    )
    parser.add_argument(
        "--recover-event-id",
        default=None,
        help="Read one historical canary row without another append",
    )
    parser.add_argument("--operator-authorization-ref", required=True)
    parser.add_argument(
        "--authorize-live-canary",
        action="store_true",
        help="Authorize isolated KG reads and one neutral audit.events write/read",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.authorize_live_canary:
        print(
            "REFUSED: --authorize-live-canary is required; no live operations attempted",
            file=sys.stderr,
        )
        return 2
    try:
        receipt = asyncio.run(
            run_live_canary(
                enrollment_path=args.enrollment.resolve(),
                tasks_path=args.tasks.resolve(),
                source_map_path=args.source_map.resolve(),
                offline_receipt_path=args.offline_receipt.resolve(),
                mcp_url=args.mcp_url,
                timeout_s=args.timeout_s,
                authorization_ref=args.operator_authorization_ref,
                probe_evidence_path=(
                    args.probe_evidence.resolve()
                    if args.probe_evidence is not None
                    else None
                ),
                recover_event_id=args.recover_event_id,
            )
        )
        v0.write_content_addressed_receipt(args.receipt.resolve(), receipt)
    except (
        v0.LiveCanaryError,
        v0.ProtocolError,
        OSError,
        ValueError,
        ExceptionGroup,
    ) as exc:
        print(f"LIVE CANARY V1 FAILED: {exc}", file=sys.stderr)
        return 1
    print(v0.canonical_json(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
