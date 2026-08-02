#!/usr/bin/env python3
"""Dialectic one-call canary — the positive control for the #1387 kill-gate read.

Why this exists: the adoption kill-gate reads "organic request_review usage
stayed at zero" as a disinterest signal. That zero has now been produced three
times by defects in the surface itself, never once by demonstrated disinterest:
#1414 (auth resolved the public handle, real callers refused), #1424 (the
/mcp+/sse transport was never instrumented, real usage invisible), #1442 (the
one-call form timed out at 60s persisting nothing). A zero is only evidence
when the channel is proven live — so this canary exercises the exact surface
the gate measures, end-to-end, on a schedule:

  1. onboard a fresh identity over /mcp (the transport real agents use),
     named so `core.agents.label` starts with ``canary_`` — the exclusion
     pattern `label LIKE 'canary\\_%'` keeps probe traffic out of
     adoption_kpi.py and out of the gate-read SQL. The label prefix is
     ASSERTED after onboard: if the server ever composes labels differently,
     the canary fails loudly instead of silently polluting the organic count.
  2. call one-call request_review (issue_description + reasoning + root_cause
     + proposed_conditions) and evaluate the response shape.
  3. ground-truth the DB: the session row AND the thesis message row must
     exist. #1442's failure mode left neither — a response-level check alone
     would have needed the timeout error to say so, and pre-#1424 no
     telemetry said anything.
  4. append one JSONL line per run; exit 0/1 (launchd surfaces the log).

Gate contract (#1387 amendment, 2026-08-01): the kill read is valid only if
this canary is green through the measurement window. Organic zero + green
canary = the one-call lever didn't move the dial → retire the LEVER, iterate
to the next one (per the adoption lever model). Organic zero + red canary =
instrument failure → the clock restarts from the fix's deploy; no kill.

Usage:
    python3 scripts/ops/dialectic_canary.py [--url URL] [--log PATH] [--skip-db]

Env:
    UNITARES_MCP_URL              default http://127.0.0.1:8767/mcp/
    UNITARES_DIALECTIC_CANARY_LOG default <repo>/data/logs/dialectic_canary.jsonl
    GOVERNANCE_DATABASE_URL       default postgresql://postgres:postgres@localhost:5432/governance
    UNITARES_CANARY_TIMEOUT_S     default 150 (must clear the 105s one-call ceiling)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = REPO_ROOT / "data" / "logs" / "dialectic_canary.jsonl"
DEFAULT_URL = os.environ.get("UNITARES_MCP_URL", "http://127.0.0.1:8767/mcp/")
CANARY_NAME = "canary_dialectic"
LABEL_PREFIX = "canary_"


def _timeout_s() -> float:
    raw = os.environ.get("UNITARES_CANARY_TIMEOUT_S")
    try:
        return float(raw) if raw else 150.0
    except ValueError:
        return 150.0


async def call_tool(
    url: str, tool_name: str, arguments: Dict[str, Any], timeout_s: float
) -> Dict[str, Any]:
    """One streamable-HTTP tool call, fresh transport per call.

    Deliberately NOT scripts/ops/mcp_agent.py: its client pins a 30s httpx
    timeout, which would kill a legitimate one-call review (ceiling 105s,
    #1442) and turn every canary run into a false red. Identity continuity
    across the two calls rides on client_session_id, not the transport.
    """
    import httpx
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    http_client = httpx.AsyncClient(timeout=timeout_s)
    async with streamable_http_client(url, http_client=http_client) as streams:
        read, write, _ = streams
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            for content in result.content:
                text = getattr(content, "text", None)
                if not text:
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    return data
    return {"success": False, "error": "no JSON content in tool response"}


def evaluate_onboard(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Pure check: fresh identity minted AND the label carries the exclusion
    prefix the KPI/gate SQL filters on."""
    if payload.get("success") is not True:
        return False, f"onboard failed: {payload.get('error', payload)}"
    raw = payload.get("raw_governance") or payload
    uuid = payload.get("agent_uuid") or raw.get("uuid")
    if not uuid:
        return False, "onboard returned no uuid"
    label = raw.get("display_name") or payload.get("display_name") or ""
    if not label.startswith(LABEL_PREFIX):
        return False, (
            f"label {label!r} lacks the {LABEL_PREFIX!r} prefix — the KPI "
            "exclusion would not match and canary traffic would pollute the "
            "organic count; refusing to proceed"
        )
    return True, "ok"


def review_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The MCP alias envelope nests the handler's JSON under raw_governance
    (top level carries success/tool/state_summary convenience fields). Read
    the handler payload; fall back to the flat shape for direct callers."""
    raw = payload.get("raw_governance")
    if isinstance(raw, dict) and ("session_id" in raw or "error" in raw):
        return raw
    return payload


def evaluate_review(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Pure check of the one-call response shape (#1385 contract)."""
    if payload.get("success") is not True:
        return False, f"request_review failed: {payload.get('error', payload)}"
    raw = review_payload(payload)
    if raw.get("success") is not True:
        return False, f"request_review failed: {raw.get('error', raw)}"
    if not raw.get("session_id"):
        return False, "no session_id in response"
    if raw.get("thesis_recorded") is False:
        return False, "session created but thesis NOT recorded (#1414 shape)"
    if raw.get("one_call_review") is not True:
        return False, "one_call_review branch did not run"
    return True, "ok"


def db_ground_truth(session_id: str) -> Tuple[bool, str]:
    """The response can lie by omission; the rows cannot. #1442 left NO rows."""
    import psycopg2  # type: ignore

    dsn = os.environ.get(
        "GOVERNANCE_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/governance",
    )
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM core.dialectic_sessions WHERE session_id = %s",
                (session_id,),
            )
            if cur.fetchone() is None:
                return False, "no core.dialectic_sessions row"
            cur.execute(
                "SELECT count(*) FROM core.dialectic_messages "
                "WHERE session_id = %s AND message_type = 'thesis'",
                (session_id,),
            )
            if (cur.fetchone() or [0])[0] < 1:
                return False, "session row exists but no thesis message row"
    return True, "ok"


def append_log(log_path: Path, record: Dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


async def run(url: str, log_path: Path, skip_db: bool) -> int:
    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ok": False,
        "stage": "onboard",
        "url": url,
    }
    started = time.monotonic()
    timeout_s = _timeout_s()
    try:
        onboard = await call_tool(
            url,
            "start_session",
            {"force_new": True, "name": CANARY_NAME, "spawn_reason": "new_session"},
            timeout_s=60.0,
        )
        ok, detail = evaluate_onboard(onboard)
        raw = onboard.get("raw_governance") or onboard
        record["agent_uuid"] = onboard.get("agent_uuid") or raw.get("uuid")
        record["label"] = raw.get("display_name")
        if not ok:
            record["detail"] = detail
            return 1

        record["stage"] = "request_review"
        csid = onboard.get("client_session_id") or raw.get("client_session_id")
        t0 = time.monotonic()
        review = await call_tool(
            url,
            "request_review",
            {
                "client_session_id": csid,
                "issue_description": (
                    "Scheduled canary probe: verifying the one-call review "
                    "surface end-to-end (#1387 positive control)."
                ),
                "reasoning": (
                    "This is the daily positive control for the adoption "
                    "kill-gate. A zero organic count is only evidence of "
                    "disinterest if this exact call path works."
                ),
                "root_cause": "canary probe — no real incident",
                "proposed_conditions": ["log the probe result", "exit"],
            },
            timeout_s=timeout_s,
        )
        record["latency_s"] = round(time.monotonic() - t0, 2)
        ok, detail = evaluate_review(review)
        raw_review = review_payload(review)
        record["session_id"] = raw_review.get("session_id")
        record["review_verdict"] = raw_review.get("review_verdict")
        record["whose_move"] = raw_review.get("whose_move")
        record["orchestrated"] = raw_review.get("orchestrated_review")
        if not ok:
            record["detail"] = detail
            return 1

        if not skip_db:
            record["stage"] = "db_ground_truth"
            ok, detail = db_ground_truth(raw_review["session_id"])
            if not ok:
                record["detail"] = detail
                return 1

        record["stage"] = "done"
        record["ok"] = True
        record["detail"] = "ok"
        return 0
    except Exception as exc:  # noqa: BLE001 — the canary reports, never raises
        record["detail"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        record["total_s"] = round(time.monotonic() - started, 2)
        append_log(log_path, record)
        print(json.dumps(record, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--log",
        default=os.environ.get("UNITARES_DIALECTIC_CANARY_LOG", str(DEFAULT_LOG)),
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip the Postgres ground-truth check (response-shape only)",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.url, Path(args.log), args.skip_db))


if __name__ == "__main__":
    sys.exit(main())
