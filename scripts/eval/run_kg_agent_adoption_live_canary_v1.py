#!/usr/bin/env python3
"""Run the isolated UNITARES adoption infrastructure canary v1.

Version 0 is immutable evidence for the 2026-08-27 production-plugin probe.
This follow-up preserves its frozen retrieval contract while closing these seams:

* a fresh canary may run only through the full loopback MCP endpoint from this
  standalone process; captured in-slot plugin evidence is recovery-only;
* audit readback uses the canonical audit query API, which returns normalized
  JSON payloads and supports exact event-ID filtering;
* details reads preserve both complete nested records and paginated text,
  within the frozen root's two-page read budget;
* search responses are validated before another request is allowed, and an
  optional private progress log retains bounded evidence across failures.

The runner remains HOLD-only. It does not call a model, write the KG, record an
outcome, execute a scored task, invoke orchestration, or surface an alert.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import stat
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
_MAX_DETAIL_PAGES = 2  # Frozen root: 2717 characters, requested in 2000-char pages.
_PROGRESS_LIMIT = 128 * 1024


def _checkpoint(path: Path | None, stage: str, **fields: Any) -> None:
    """Durably retain bounded metadata before a possibly ambiguous operation."""
    if path is None:
        return
    line = (json.dumps({"stage": stage, **fields}, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        v0._require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600,
            "progress log must be an owned private regular file",
        )
        v0._require(info.st_size + len(line) <= _PROGRESS_LIMIT, "progress log limit exceeded")
        with os.fdopen(fd, "ab", closefd=False) as stream:
            stream.write(line)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


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


async def _fetch_discovery(
    url: str,
    discovery_id: str,
    *,
    client_session_id: str,
    timeout_s: float,
    progress_path: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Read the frozen root without dropping the unpaginated details field."""
    offset = 0
    parts: list[str] = []
    metadata: dict[str, Any] | None = None
    for page_index in range(_MAX_DETAIL_PAGES):
        payload = await v0._call_mcp_tool(
            url,
            "knowledge",
            {
                "action": "details",
                "discovery_id": discovery_id,
                "offset": offset,
                "length": 2000,
                "include_response_chain": False,
                "response_mode": "compact",
                "client_session_id": client_session_id,
            },
            timeout_s=timeout_s,
        )
        _checkpoint(progress_path, "details_received", offset=offset,
                    payload_sha256=v0.sha256_json(payload))
        v0._require(payload.get("success") is True, "knowledge details failed")
        record = payload.get("discovery")
        v0._require(
            isinstance(record, Mapping) and record.get("id") == discovery_id,
            "knowledge details returned the wrong or missing discovery",
        )
        pagination = payload.get("pagination", {})
        v0._require(isinstance(pagination, Mapping), "invalid KG pagination")
        # The server nests the complete text when no pagination is needed.
        # A paginated page must supply its own slice, never the full record.
        details = payload.get("details") if pagination else payload.get(
            "details", record.get("details")
        )
        v0._require(isinstance(details, str), "KG details text is missing or invalid")
        if metadata is None:
            metadata = dict(record)
        parts.append(details)
        has_more = pagination.get("has_more", False)
        v0._require(isinstance(has_more, bool), "invalid KG has_more flag")
        if not has_more:
            metadata["details"] = "".join(parts)
            return metadata, page_index + 1
        next_offset = pagination.get("next_offset")
        v0._require(
            isinstance(next_offset, int)
            and not isinstance(next_offset, bool)
            and bool(details)
            and next_offset == offset + len(details),
            "invalid KG pagination offset",
        )
        offset = next_offset
    raise v0.LiveCanaryError("KG details exceeded the frozen two-page read budget")


async def _run_live_searches(
    url: str,
    tasks: Mapping[str, Any],
    source_map: Mapping[str, Any],
    *,
    client_session_id: str,
    timeout_s: float,
    progress_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Validate one frozen search before permitting the next request."""
    results: dict[str, dict[str, Any]] = {}
    for chain in tasks["chains"]:
        for step in chain["steps"]:
            if not step["eligible_for_prior_work"]:
                continue
            payload = await v0._call_mcp_tool(
                url, "knowledge",
                {
                    "action": v0.RETRIEVAL_CONTRACT["action"],
                    "query": step["injection_query"],
                    "search_mode": v0.RETRIEVAL_CONTRACT["search_mode"],
                    "operator": v0.RETRIEVAL_CONTRACT["operator"],
                    "limit": v0.RETRIEVAL_CONTRACT["limit"],
                    "include_details": v0.RETRIEVAL_CONTRACT["include_details"],
                    "include_archived": v0.RETRIEVAL_CONTRACT["include_archived"],
                    "response_mode": "full",
                    "client_session_id": client_session_id,
                },
                timeout_s=timeout_s,
            )
            step_id = step["step_id"]
            _checkpoint(progress_path, "search_received", step_id=step_id,
                        payload_sha256=v0.sha256_json(payload))
            rows = payload.get("discoveries")
            v0._require(
                isinstance(rows, list)
                and len(rows) <= v0.RETRIEVAL_CONTRACT["limit"]
                and all(isinstance(row, Mapping)
                        and isinstance(row.get("id"), str) and row["id"] for row in rows),
                f"invalid discovery rows for {step_id}",
            )
            ids = [row["id"] for row in rows]
            v0._require(len(ids) == len(set(ids)), f"duplicate discovery rows for {step_id}")
            # This is a validation view, not a new task/enrollment. A rank miss
            # remains adverse evidence; refused or degraded responses stop here.
            check = v0.summarize_search_results(
                {"chains": [{"steps": [step]}]}, source_map, {step_id: payload},
            )[0]
            _checkpoint(progress_path, "search_validated", **check)
            results[step_id] = payload
    return results


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
    progress_path: Path | None = None,
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
    _checkpoint(progress_path, "audit_append_prepared", event_id=event_id,
                content_sha256=content_digest)
    appended = await append_audit_event_async(entry, raw_hash=content_digest)
    _checkpoint(progress_path, "audit_append_returned", event_id=event_id,
                appended=appended is True)
    v0._require(appended is True, "awaited audit append returned false")
    rows = await _query_exact_audit_event(event_id)
    result = v0.validate_audit_readback(
        event_id=event_id,
        expected_details=details,
        rows=rows,
    )
    _checkpoint(progress_path, "audit_readback_verified", **result)
    return result


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


def _stored_session_identity(identity: Mapping[str, str]) -> dict[str, str]:
    """``identity`` with its session id as audit.tool_usage stores it.

    A keyed client_session_id is a credential, so the server records its
    ``csid:`` digest; v0's attribution reader matches rows on the stored
    form. v0 stays byte-identical to its attested runner, so the mapping
    lives here.
    """
    from src.mcp_handlers.identity.stable_session import audit_reference

    return {**identity, "client_session_id": audit_reference(identity["client_session_id"])}


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
    progress_path: Path | None = None,
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

    if progress_path is not None:
        parent = progress_path.parent.stat()
        v0._require(
            parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) == 0o700,
            "progress directory must be owned and mode 0700",
        )
        fd = os.open(progress_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        _checkpoint(progress_path, "preflight_validated")
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
        _checkpoint(progress_path, "identity_bound", **identity)
        exclusion_contract = v0.validate_measurement_exclusion_contract(
            identity["label"]
        )
        if probe_evidence is None:
            discovery, details_pages = await _fetch_discovery(
                mcp_url,
                source_map["discovery"]["id"],
                client_session_id=identity["client_session_id"],
                timeout_s=timeout_s,
                progress_path=progress_path,
            )
        projection = v0.validate_source_map(tasks, source_map, discovery)
        _checkpoint(progress_path, "source_projection_verified", **projection)
        if probe_evidence is None:
            search_payloads = await _run_live_searches(
                mcp_url,
                tasks,
                source_map,
                client_session_id=identity["client_session_id"],
                timeout_s=timeout_s,
                progress_path=progress_path,
            )
        search_checks = v0.summarize_search_results(
            tasks,
            source_map,
            search_payloads,
        )
        attribution = await v0._await_tool_usage_attribution(
            identity=_stored_session_identity(identity),
            started_at=started_at,
            expected_searches=len(search_checks),
            expected_details=details_pages,
        )
        calibration_exclusion = await v0._validate_calibration_exclusion(
            identity=identity,
            started_at=started_at,
        )
        _checkpoint(progress_path, "postflight_validated")
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
                progress_path=progress_path,
            )
    except BaseException as exc:
        _checkpoint(progress_path, "failed", exception_type=type(exc).__name__)
        raise
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
    parser.add_argument("--progress", type=Path, default=None,
                        help="New private metadata log in an owned mode-0700 directory")
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
                progress_path=args.progress.resolve() if args.progress else None,
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
