#!/usr/bin/env python3
"""Run the operator-authorized UNITARES adoption infrastructure canary.

The offline adoption runner stays network- and production-write-free. This
separate runner reads a pinned live KG root, records six frozen-query ranks,
and appends exactly one neutral audit.events canary row before exact readback.
It never calls a model, writes the KG, records an outcome, runs a scored task,
or invokes orchestration. The receipt stays HOLD while corpus parity is absent.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
import uuid

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval.kg_agent_adoption import (  # noqa: E402
    ProtocolError,
    canonical_json,
    sha256_json,
    validate_task_chains,
)
from scripts.eval.run_kg_agent_adoption import (  # noqa: E402
    DEFAULT_ENROLLMENT,
    DEFAULT_TASKS,
    _load_json,
    sha256_file,
    validate_enrollment,
    write_content_addressed_receipt,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_MAP = (
    REPO_ROOT / "docs/evaluations/kg-agent-adoption/live-source-map-v0.json"
)
DEFAULT_OFFLINE_RECEIPT = (
    REPO_ROOT
    / "docs/evaluations/kg-agent-adoption/offline-fixture-canary-v0.receipt.json"
)
DEFAULT_LIVE_RECEIPT = (
    REPO_ROOT
    / "docs/evaluations/kg-agent-adoption/live-plumbing-canary-v0.receipt.json"
)
SOURCE_MAP_SCHEMA = "unitares.kg-agent-adoption.live-source-map.v0"
LIVE_PROJECTION_SCHEMA = "unitares.kg-agent-adoption.live-search-projection.v0"
LIVE_RECEIPT_SCHEMA = "unitares.kg-agent-adoption.live-canary.v0"
AUDIT_CONTENT_SCHEMA = "unitares.infrastructure-audit-canary-content.v0"
PROBE_EVIDENCE_SCHEMA = "unitares.kg-agent-adoption.live-mcp-probe.v0"
AUDIT_EVENT_TYPE = "infrastructure.audit_write_read_canary.v1"
CANARY_LABEL_PREFIX = "canary_agent_adoption"
RETRIEVAL_CONTRACT = {
    "tool": "knowledge",
    "action": "search",
    "search_mode": "fts",
    "operator": "OR",
    "limit": 5,
    "include_details": True,
    "include_archived": False,
    "reranker_enabled": False,
}
_SHA256_HEX = frozenset("0123456789abcdef")


class LiveCanaryError(RuntimeError):
    """Fail-closed live canary protocol error."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LiveCanaryError(message)


def _require_sha256(value: Any, *, where: str) -> str:
    _require(
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= _SHA256_HEX,
        f"{where} must be a lowercase SHA-256 digest",
    )
    return value


def _review_block(document: Mapping[str, Any]) -> str:
    review = document["review"]
    return (
        '  "review": {\n'
        f'    "status": {json.dumps(review["status"])},\n'
        f'    "scope": {json.dumps(review["scope"])},\n'
        '    "governed_review_session": '
        f"{json.dumps(review['governed_review_session'])},\n"
        f'    "approved_by": {json.dumps(review["approved_by"])}\n'
        "  },"
    )


def _pre_promotion_enrollment_digest(
    enrollment_path: Path, enrollment: Mapping[str, Any]
) -> str:
    """Recreate the reviewed receipt's exact pre-promotion enrollment bytes."""
    text = enrollment_path.read_text(encoding="utf-8")
    promoted = _review_block(enrollment)
    unreviewed = (
        '  "review": {\n'
        '    "status": "unreviewed",\n'
        '    "scope": null,\n'
        '    "governed_review_session": null,\n'
        '    "approved_by": null\n'
        "  },"
    )
    _require(
        text.count(promoted) == 1,
        "review metadata block is not the canonical four-field promoted shape",
    )
    return hashlib.sha256(
        text.replace(promoted, unreviewed, 1).encode("utf-8")
    ).hexdigest()


def validate_reviewed_offline_basis(
    enrollment: Mapping[str, Any],
    *,
    enrollment_path: Path,
    tasks: Mapping[str, Any],
    tasks_path: Path,
    offline_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the live canary to the independently reviewed offline artifact."""
    validation = validate_enrollment(
        enrollment,
        tasks_path=tasks_path,
        task_document=tasks,
    )
    review = enrollment["review"]
    _require(
        review["status"] == "offline_fixture_reviewed"
        and review["scope"] == "offline_fixture_validation"
        and isinstance(review["governed_review_session"], str)
        and review["governed_review_session"]
        and isinstance(review["approved_by"], str)
        and review["approved_by"],
        "live canary requires an independently reviewed offline fixture",
    )
    _require(
        enrollment["model"]["live_calls_authorized"] is False
        and enrollment["execution"]["scored_live_model_runs_authorized"] is False
        and enrollment["execution"]["writes_authorized"] is False,
        "behavioral/model execution flags must remain false",
    )
    _require(
        offline_receipt.get("schema") == "unitares.content-addressed-receipt.v0",
        "offline receipt envelope schema mismatch",
    )
    content = offline_receipt.get("content")
    _require(isinstance(content, Mapping), "offline receipt content is missing")
    content_digest = _require_sha256(
        offline_receipt.get("receipt_content_sha256"),
        where="offline receipt content digest",
    )
    _require(
        sha256_json(content) == content_digest,
        "offline receipt content digest mismatch",
    )
    claims = content.get("claims", {})
    _require(
        content.get("status") == "passed"
        and content.get("scope") == "offline_fixture_validation"
        and claims.get("offline_fixture_passed") is True,
        "offline receipt does not prove the reviewed fixture scope",
    )
    _require(
        claims.get("behavioral_evidence") is False
        and claims.get("scored_execution_authorized") is False
        and claims.get("live_execution_authorized") is False,
        "offline receipt contains an unauthorized live or behavioral claim",
    )
    _require(
        content.get("digests", {}).get("task_manifest_sha256")
        == sha256_file(tasks_path),
        "offline receipt task digest does not match the current fixture",
    )
    _require(
        content.get("digests", {}).get("enrollment_sha256")
        == _pre_promotion_enrollment_digest(enrollment_path, enrollment),
        "promoted enrollment differs materially from the reviewed artifact",
    )
    return {
        **validation,
        "review_session": review["governed_review_session"],
        "approved_by": review["approved_by"],
        "offline_receipt_content_sha256": content_digest,
        "frozen_digests": dict(content["digests"]),
    }


def canonical_live_projection(discovery: Mapping[str, Any]) -> dict[str, Any]:
    required = {"id", "type", "status", "summary", "tags", "details"}
    missing = required - set(discovery)
    _require(not missing, f"live discovery missing fields: {sorted(missing)}")
    _require(isinstance(discovery["tags"], list), "live discovery tags must be a list")
    return {
        "schema": LIVE_PROJECTION_SCHEMA,
        "discovery_id": discovery["id"],
        "type": discovery["type"],
        "status": discovery["status"],
        "summary": discovery["summary"],
        "details": discovery["details"],
        "tags": sorted(discovery["tags"]),
    }


def validate_measurement_exclusion_contract(label: str) -> dict[str, Any]:
    """Pin the live canary label to the adoption KPI's real exclusion paths."""
    from scripts.dev.adoption_kpi import _scheduled_label_re, _snapshot_queries

    scheduled_pattern = _scheduled_label_re()
    queries = _snapshot_queries()
    _require(
        re.search(scheduled_pattern, label, flags=re.IGNORECASE) is not None,
        "canary label is not excluded from scheduled surface-return telemetry",
    )
    literal_filter = "NOT LIKE 'canary\\_%%'"
    for query_name in ("agent_kg_retrieval", "onboard_conversion"):
        _require(
            literal_filter in queries[query_name],
            f"{query_name} no longer excludes canary labels",
        )
    return {
        "label": label,
        "scheduled_label_regex_sha256": hashlib.sha256(
            scheduled_pattern.encode("utf-8")
        ).hexdigest(),
        "agent_kg_retrieval_query_sha256": hashlib.sha256(
            queries["agent_kg_retrieval"].encode("utf-8")
        ).hexdigest(),
        "onboard_conversion_query_sha256": hashlib.sha256(
            queries["onboard_conversion"].encode("utf-8")
        ).hexdigest(),
        "excluded_from_adoption_kpis": True,
    }


def validate_source_map(
    tasks: Mapping[str, Any],
    source_map: Mapping[str, Any],
    discovery: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a derived, explicitly non-byte-equivalent four-to-one map."""
    validated_tasks = validate_task_chains(tasks)
    _require(source_map.get("schema") == SOURCE_MAP_SCHEMA, "source-map schema mismatch")
    _require(
        source_map.get("relation") == "derived_projection"
        and source_map.get("byte_equivalent") is False,
        "source map must declare derived_projection and byte_equivalent=false",
    )
    projection = canonical_live_projection(discovery)
    discovery_binding = source_map.get("discovery")
    _require(isinstance(discovery_binding, Mapping), "source-map discovery missing")
    _require(
        discovery_binding.get("id") == projection["discovery_id"],
        "source-map discovery ID mismatch",
    )
    expected_record_digest = _require_sha256(
        discovery_binding.get("record_sha256"), where="live projection digest"
    )
    _require(
        sha256_json(projection) == expected_record_digest,
        "live KG root drifted from the frozen search projection",
    )
    _require(projection["status"] == "open", "live KG root is not open")

    corpus = {item["source_id"]: item for item in validated_tasks["substitute_corpus"]}
    mappings = source_map.get("logical_sources")
    _require(isinstance(mappings, list), "source-map logical_sources must be a list")
    _require(len(mappings) == len(corpus), "source-map source count mismatch")
    mapped: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(mappings):
        _require(isinstance(item, Mapping), f"source-map item {index} must be an object")
        expected_fields = {
            "source_id",
            "discovery_id",
            "relation",
            "byte_equivalent",
            "support_field",
            "support_fragment",
            "support_sha256",
            "logical_source_sha256",
        }
        _require(
            set(item) == expected_fields,
            f"source-map item {index} fields differ from the closed schema",
        )
        source_id = item["source_id"]
        _require(source_id in corpus, f"source-map contains unknown source {source_id}")
        _require(source_id not in mapped, f"duplicate source-map entry {source_id}")
        _require(
            item["discovery_id"] == projection["discovery_id"]
            and item["relation"] == "derived_projection"
            and item["byte_equivalent"] is False,
            f"source {source_id} makes a false equivalence or maps elsewhere",
        )
        field = item["support_field"]
        _require(field in {"summary", "details"}, f"invalid support field for {source_id}")
        fragment = item["support_fragment"]
        _require(
            isinstance(fragment, str) and fragment and fragment in projection[field],
            f"support fragment for {source_id} is absent from live {field}",
        )
        _require(
            hashlib.sha256(fragment.encode("utf-8")).hexdigest()
            == _require_sha256(item["support_sha256"], where=f"{source_id} support"),
            f"support fragment digest mismatch for {source_id}",
        )
        _require(
            sha256_json(corpus[source_id])
            == _require_sha256(
                item["logical_source_sha256"], where=f"{source_id} logical source"
            ),
            f"frozen logical source drifted for {source_id}",
        )
        mapped[source_id] = dict(item)
    _require(set(mapped) == set(corpus), "source map does not cover the corpus")
    mapping_digest = _require_sha256(
        source_map.get("mapping_sha256"), where="source-map mapping digest"
    )
    _require(
        sha256_json(mappings) == mapping_digest,
        "source-map mapping digest mismatch",
    )
    return {
        "record_sha256": expected_record_digest,
        "mapping_sha256": mapping_digest,
        "relation": "derived_projection",
        "byte_equivalent": False,
        "logical_source_ids": sorted(mapped),
    }


def summarize_search_results(
    tasks: Mapping[str, Any],
    source_map: Mapping[str, Any],
    payloads_by_step: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Record frozen-query ranks; top-five misses are evidence, not exceptions."""
    mappings = {
        item["source_id"]: item["discovery_id"]
        for item in source_map["logical_sources"]
    }
    checks: list[dict[str, Any]] = []
    for chain in tasks["chains"]:
        for step in chain["steps"]:
            if not step["eligible_for_prior_work"]:
                continue
            step_id = step["step_id"]
            payload = payloads_by_step.get(step_id)
            _require(isinstance(payload, Mapping), f"missing search payload for {step_id}")
            _require(payload.get("success") is True, f"search failed for {step_id}")
            _require(
                payload.get("search_mode_used") == "fts"
                and payload.get("operator_used") == "OR"
                and payload.get("search_mode_requested") == "fts",
                f"search contract drift for {step_id}",
            )
            _require(
                payload.get("search_degraded") is not True
                and payload.get("fallback_used") is not True,
                f"degraded/fallback search is not parity evidence for {step_id}",
            )
            rows = payload.get("discoveries")
            _require(isinstance(rows, list), f"invalid discoveries for {step_id}")
            ordered_ids = [
                row["id"] for row in rows if isinstance(row, Mapping) and "id" in row
            ]
            _require(len(ordered_ids) <= RETRIEVAL_CONTRACT["limit"], "search exceeded K")
            expected_discoveries = sorted(
                {mappings[source] for source in step["answer_key"]["material_source_ids"]}
            )
            ranks = {
                discovery_id: (
                    ordered_ids.index(discovery_id) + 1
                    if discovery_id in ordered_ids
                    else None
                )
                for discovery_id in expected_discoveries
            }
            checks.append(
                {
                    "step_id": step_id,
                    "query_sha256": hashlib.sha256(
                        step["injection_query"].encode("utf-8")
                    ).hexdigest(),
                    "expected_logical_source_ids": step["answer_key"][
                        "material_source_ids"
                    ],
                    "expected_discovery_ids": expected_discoveries,
                    "returned_discovery_ids": ordered_ids,
                    "returned_record_sha256s": [sha256_json(row) for row in rows],
                    "canonical_ranks": ranks,
                    "passed": all(rank is not None for rank in ranks.values()),
                }
            )
    return checks


def validate_audit_readback(
    *,
    event_id: str,
    expected_details: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    _require(len(rows) == 1, f"expected one audit row for {event_id}, got {len(rows)}")
    row = rows[0]
    _require(str(row.get("event_id")) == event_id, "audit event ID mismatch")
    _require(row.get("event_type") == AUDIT_EVENT_TYPE, "audit event type mismatch")
    _require(row.get("agent_id") is None, "audit canary must not carry an agent ID")
    _require(row.get("session_id") is None, "audit canary must not carry a session ID")
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LiveCanaryError("audit readback payload is not valid JSON") from exc
    _require(payload == expected_details, "audit readback payload mismatch")
    content = expected_details.get("content")
    _require(isinstance(content, Mapping), "audit canary content missing")
    content_digest = _require_sha256(
        expected_details.get("content_sha256"), where="audit content digest"
    )
    _require(sha256_json(content) == content_digest, "audit content digest mismatch")
    _require(row.get("raw_hash") == content_digest, "audit raw hash mismatch")
    return {
        "event_id": event_id,
        "event_type": AUDIT_EVENT_TYPE,
        "append_attempts": 1,
        "matching_rows": 1,
        "append_awaited": True,
        "read_awaited": True,
        "exact_readback": True,
        "immediate_readback_exact": True,
        "recovery_used": False,
        "content_sha256": content_digest,
    }


async def _call_mcp_tool(
    url: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from src.mcp_compat import mcp_httpx

    http_client = mcp_httpx().AsyncClient(timeout=timeout_s)
    async with streamable_http_client(url, http_client=http_client) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, dict(arguments))
            for content in result.content:
                text = getattr(content, "text", None)
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    return payload
    raise LiveCanaryError(f"{tool_name} returned no JSON object")


async def _mint_canary_identity(url: str, timeout_s: float) -> dict[str, str]:
    payload = await _call_mcp_tool(
        url,
        "start_session",
        {
            "force_new": True,
            "name": CANARY_LABEL_PREFIX,
            "client_hint": "KG adoption production-plumbing canary",
            "response_mode": "minimal",
        },
        timeout_s=timeout_s,
    )
    _require(payload.get("success") is True, f"canary onboarding failed: {payload}")
    raw = payload.get("raw_governance") or payload
    identity = payload.get("agent_uuid") or raw.get("uuid")
    client_session_id = payload.get("client_session_id") or raw.get("client_session_id")
    label = raw.get("display_name") or payload.get("display_name")
    _require(isinstance(identity, str) and identity, "onboarding returned no UUID")
    _require(
        isinstance(client_session_id, str) and client_session_id,
        "onboarding returned no client_session_id",
    )
    _require(
        isinstance(label, str) and label.startswith(CANARY_LABEL_PREFIX),
        f"label {label!r} would not be excluded by the canary partition",
    )
    return {
        "agent_uuid": identity,
        "client_session_id": client_session_id,
        "label": label,
    }


async def _fetch_discovery(
    url: str,
    discovery_id: str,
    *,
    client_session_id: str,
    timeout_s: float,
) -> tuple[dict[str, Any], int]:
    offset = 0
    details_parts: list[str] = []
    metadata: dict[str, Any] | None = None
    pages = 0
    while True:
        payload = await _call_mcp_tool(
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
        _require(payload.get("success") is True, f"knowledge details failed: {payload}")
        if metadata is None:
            metadata = dict(payload["discovery"])
        details_parts.append(payload.get("details", ""))
        pages += 1
        pagination = payload.get("pagination") or {}
        if not pagination.get("has_more"):
            break
        next_offset = pagination.get("next_offset")
        _require(
            isinstance(next_offset, int) and next_offset > offset,
            "invalid KG pagination",
        )
        offset = next_offset
    _require(metadata is not None, "KG details returned no discovery")
    metadata["details"] = "".join(details_parts)
    return metadata, pages


async def _run_live_searches(
    url: str,
    tasks: Mapping[str, Any],
    *,
    client_session_id: str,
    timeout_s: float,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for chain in tasks["chains"]:
        for step in chain["steps"]:
            if not step["eligible_for_prior_work"]:
                continue
            results[step["step_id"]] = await _call_mcp_tool(
                url,
                "knowledge",
                {
                    "action": RETRIEVAL_CONTRACT["action"],
                    "query": step["injection_query"],
                    "search_mode": RETRIEVAL_CONTRACT["search_mode"],
                    "operator": RETRIEVAL_CONTRACT["operator"],
                    "limit": RETRIEVAL_CONTRACT["limit"],
                    "include_details": RETRIEVAL_CONTRACT["include_details"],
                    "include_archived": RETRIEVAL_CONTRACT["include_archived"],
                    "response_mode": "full",
                    "client_session_id": client_session_id,
                },
                timeout_s=timeout_s,
            )
    return results


async def _read_canary_tool_usage(
    *, identity: Mapping[str, str], started_at: datetime
) -> list[dict[str, Any]]:
    from src.db import get_db

    db = get_db()
    if not hasattr(db, "_pool") or db._pool is None:
        await db.init()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.ts, u.agent_id, u.session_id, u.tool_name, u.payload, a.label
            FROM audit.tool_usage u
            LEFT JOIN core.agents a ON a.id::text = u.agent_id
            WHERE u.ts >= $1
              AND u.tool_name = 'knowledge'
              AND (u.agent_id = $2 OR u.session_id = $3)
            ORDER BY u.ts ASC
            """,
            started_at,
            identity["agent_uuid"],
            identity["client_session_id"],
        )
    return [dict(row) for row in rows]


def validate_tool_usage_attribution(
    *,
    identity: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
    expected_searches: int,
    expected_details: int,
) -> dict[str, Any]:
    _require(rows, "canary MCP traffic was not recorded in audit.tool_usage")
    for row in rows:
        _require(
            row.get("agent_id") == identity["agent_uuid"]
            and row.get("session_id") == identity["client_session_id"]
            and isinstance(row.get("label"), str)
            and row["label"].startswith(CANARY_LABEL_PREFIX),
            "canary tool usage is missing canary-labelled identity attribution",
        )
    actions = []
    for row in rows:
        if row.get("tool_name") != "knowledge":
            continue
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise LiveCanaryError(
                    "canary tool_usage payload is not valid JSON"
                ) from exc
        _require(
            isinstance(payload, Mapping),
            "canary tool_usage payload is not an object",
        )
        actions.append(payload.get("action"))
    _require(
        actions.count("search") == expected_searches,
        f"expected {expected_searches} recorded KG searches, got {actions.count('search')}",
    )
    _require(
        actions.count("details") == expected_details,
        f"expected {expected_details} recorded KG detail reads, got {actions.count('details')}",
    )
    _require(
        len(actions) == expected_searches + expected_details,
        "unexpected extra canary KG tool_usage rows were recorded",
    )
    return {
        "agent_uuid": identity["agent_uuid"],
        "client_session_id": identity["client_session_id"],
        "label": identity["label"],
        "partition_rule": "core.agents.label LIKE 'canary_agent_adoption%'",
        "recorded_rows": len(rows),
        "recorded_searches": actions.count("search"),
        "recorded_detail_reads": actions.count("details"),
        "all_rows_attributed": True,
    }


async def _validate_calibration_exclusion(
    *,
    identity: Mapping[str, str],
    started_at: datetime,
) -> dict[str, Any]:
    """Prove a point-in-time zero; this does not rule out delayed producers."""
    from src.db import get_db

    db = get_db()
    if not hasattr(db, "_pool") or db._pool is None:
        await db.init()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              (
                SELECT count(*)
                FROM core.agent_state s
                JOIN core.identities i ON i.identity_id = s.identity_id
                WHERE i.agent_id = $1
                  AND s.synthetic = false
                  AND s.recorded_at >= $2
              ) AS measured_state_rows,
              (
                SELECT count(*)
                FROM audit.outcome_events o
                WHERE o.agent_id = $1 AND o.ts >= $2
              ) AS outcome_rows,
              (
                SELECT count(*)
                FROM audit.events e
                WHERE e.agent_id = $1
                  AND e.ts >= $2
                  AND e.event_type IN (
                    'agent_adoption.run.v1',
                    'agent_adoption.step.v1'
                  )
              ) AS adoption_event_rows
            """,
            identity["agent_uuid"],
            started_at,
        )
    result = {key: int(row[key]) for key in row.keys()}
    _require(
        all(value == 0 for value in result.values()),
        f"canary traffic contaminated calibration/adoption rows: {result}",
    )
    return {
        **result,
        "point_in_time_zero": True,
        "durable_exclusion_proven": False,
    }


async def _await_tool_usage_attribution(
    *,
    identity: Mapping[str, str],
    started_at: datetime,
    expected_searches: int,
    expected_details: int,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Bounded wait for the server's documented fire-and-forget telemetry sink."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        rows = await _read_canary_tool_usage(
            identity=identity,
            started_at=started_at,
        )
        try:
            return validate_tool_usage_attribution(
                identity=identity,
                rows=rows,
                expected_searches=expected_searches,
                expected_details=expected_details,
            )
        except LiveCanaryError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.1)


async def _query_exact_audit_event(event_id: str) -> list[dict[str, Any]]:
    from src.db import get_db

    db = get_db()
    if not hasattr(db, "_pool") or db._pool is None:
        await db.init()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ts, event_id, agent_id, session_id, event_type,
                   confidence, payload, raw_hash
            FROM audit.events
            WHERE event_id = $1
            """,
            uuid.UUID(event_id),
        )
    return [dict(row) for row in rows]


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
        "schema": AUDIT_CONTENT_SCHEMA,
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
    content_digest = sha256_json(content)
    details = {"content": content, "content_sha256": content_digest}
    entry = {
        "timestamp": created_at.isoformat(),
        "event_id": event_id,
        "event_type": AUDIT_EVENT_TYPE,
        "agent_id": None,
        "session_id": None,
        "confidence": 1.0,
        "details": details,
    }
    appended = await append_audit_event_async(entry, raw_hash=content_digest)
    _require(appended is True, "awaited audit append returned false")
    rows = await _query_exact_audit_event(event_id)
    return validate_audit_readback(
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
    """Read-only recovery for the one row whose immediate decoder failed."""
    rows = await _query_exact_audit_event(event_id)
    _require(len(rows) == 1, f"expected one recovery row for {event_id}")
    payload = rows[0].get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LiveCanaryError("recovery payload is not valid JSON") from exc
    _require(isinstance(payload, Mapping), "recovery payload is not an object")
    content = payload.get("content")
    _require(isinstance(content, Mapping), "recovery content is missing")
    created_at = content.get("created_at")
    _require(isinstance(created_at, str), "recovery created_at is missing")
    parsed_created_at = datetime.fromisoformat(created_at)
    _require(parsed_created_at.tzinfo is not None, "recovery created_at is offset-naive")
    expected_content = {
        "schema": AUDIT_CONTENT_SCHEMA,
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
    _require(content == expected_content, "recovery content differs from the closed schema")
    readback = validate_audit_readback(
        event_id=event_id,
        expected_details=payload,
        rows=rows,
    )
    return {
        **readback,
        "append_attempts": 1,
        "immediate_readback_exact": False,
        "recovery_used": True,
        "recovery_exact_readback": True,
        "recovery_reason": "database JSON payload required explicit decoding",
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
    from src.db import close_db

    enrollment = _load_json(enrollment_path)
    tasks = _load_json(tasks_path)
    source_map = _load_json(source_map_path)
    offline_receipt = _load_json(offline_receipt_path)
    reviewed = validate_reviewed_offline_basis(
        enrollment,
        enrollment_path=enrollment_path,
        tasks=tasks,
        tasks_path=tasks_path,
        offline_receipt=offline_receipt,
    )
    probe_evidence = (
        _load_json(probe_evidence_path) if probe_evidence_path is not None else None
    )
    _require(
        recover_event_id is None or probe_evidence is not None,
        "read-only audit recovery requires the bound MCP probe evidence",
    )
    if probe_evidence is not None:
        _require(
            probe_evidence.get("schema") == PROBE_EVIDENCE_SCHEMA,
            "live MCP probe evidence schema mismatch",
        )
        _require(
            probe_evidence.get("transport") == "codex-plugin-mcp",
            "probe evidence did not use the production UNITARES plugin transport",
        )
        started_at = datetime.fromisoformat(probe_evidence["started_at"])
        _require(
            started_at.tzinfo is not None,
            "probe evidence started_at must include a UTC offset",
        )
    else:
        started_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    try:
        if probe_evidence is not None:
            identity = dict(probe_evidence["identity"])
            details_pages = int(probe_evidence["details_pages"])
            _require(details_pages > 0, "probe evidence has no KG details reads")
            discovery = probe_evidence["discovery"]
            search_payloads = probe_evidence["search_payloads"]
            _require(
                isinstance(search_payloads, Mapping),
                "probe evidence search payloads are missing",
            )
        else:
            identity = await _mint_canary_identity(mcp_url, timeout_s)
        exclusion_contract = validate_measurement_exclusion_contract(identity["label"])
        if probe_evidence is None:
            discovery, details_pages = await _fetch_discovery(
                mcp_url,
                source_map["discovery"]["id"],
                client_session_id=identity["client_session_id"],
                timeout_s=timeout_s,
            )
        projection = validate_source_map(tasks, source_map, discovery)
        if probe_evidence is None:
            search_payloads = await _run_live_searches(
                mcp_url,
                tasks,
                client_session_id=identity["client_session_id"],
                timeout_s=timeout_s,
            )
        search_checks = summarize_search_results(tasks, source_map, search_payloads)
        attribution = await _await_tool_usage_attribution(
            identity=identity,
            started_at=started_at,
            expected_searches=len(search_checks),
            expected_details=details_pages,
        )
        calibration_exclusion = await _validate_calibration_exclusion(
            identity=identity,
            started_at=started_at,
        )
        enrollment_digest = sha256_file(enrollment_path)
        task_digest = sha256_file(tasks_path)
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
            "live_source_map_sha256": sha256_file(source_map_path),
            **(
                {"live_mcp_probe_sha256": sha256_file(probe_evidence_path)}
                if probe_evidence_path is not None
                else {}
            ),
        },
        "probe_transport": (
            probe_evidence["transport"] if probe_evidence is not None else "direct-mcp"
        ),
        "live_snapshot": {
            "discovery_id": source_map["discovery"]["id"],
            "projection": LIVE_PROJECTION_SCHEMA,
            "record_sha256": projection["record_sha256"],
        },
        "logical_mapping": {
            "relation": projection["relation"],
            "byte_equivalent": projection["byte_equivalent"],
            "mapping_sha256": projection["mapping_sha256"],
        },
        "retrieval_contract": {
            **RETRIEVAL_CONTRACT,
            "contract_sha256": sha256_json(RETRIEVAL_CONTRACT),
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
            "audit_append_attempts": 1,
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
            "orchestration reliability gates remain incomplete",
            *(
                ["audit canary immediate validator failed; exact row recovered read-only"]
                if audit["recovery_used"]
                else []
            ),
        ],
    }
    return {
        "schema": "unitares.content-addressed-receipt.v0",
        "receipt_content_sha256": sha256_json(content),
        "content": content,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enrollment", type=Path, default=DEFAULT_ENROLLMENT)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--source-map", type=Path, default=DEFAULT_SOURCE_MAP)
    parser.add_argument("--offline-receipt", type=Path, default=DEFAULT_OFFLINE_RECEIPT)
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
        help="Validate a captured production plugin probe before the audit append",
    )
    parser.add_argument(
        "--recover-event-id",
        default=None,
        help="Read back the already-appended canary row; never append a replacement",
    )
    parser.add_argument("--operator-authorization-ref", required=True)
    parser.add_argument(
        "--authorize-live-canary",
        action="store_true",
        help="Authorize bounded KG reads and one neutral audit.events write/read",
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
        write_content_addressed_receipt(args.receipt.resolve(), receipt)
    except (LiveCanaryError, ProtocolError, OSError, ValueError, ExceptionGroup) as exc:
        print(f"LIVE CANARY FAILED: {exc}", file=sys.stderr)
        return 1
    print(canonical_json(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
