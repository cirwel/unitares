#!/usr/bin/env python3
"""Dialectic one-call canary — terminal liveness probe for the review surface.

Originally the positive control for the #1387 adoption kill-gate. That gate is
RETIRED (2026-08-18, operator): a usage count may retire an instrument, never a
capability, because a zero cannot distinguish never-surfaced from not-reachable
from not-recorded from genuinely-unused. See "Measurement authority" in
CLAUDE.md / AGENTS.md.

The probe survives the gate on its own merit, which is the whole reason to keep
it: it is the only thing that exercises the one-call review path end-to-end on a
schedule, and it has caught real breakage three times. Its output is telemetry
about whether the surface works. It carries no authority to remove anything.

The history it was built from: that gate's zero was read as a disinterest signal. That zero has now been produced three
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
  4. poll the read-only ``dialectic(action='get')`` path until the independent
     review reaches a terminal, resolved verdict. A successful reviewer spawn
     is progress, not proof that the review completed.
  5. append one JSONL line per run; exit 0/1 (launchd surfaces the log).

Gate contract (#1387 amendment, 2026-08-01): the kill read is valid only if
this canary is green through the measurement window. Organic zero + green
canary = the one-call lever didn't move the dial → retire the LEVER, iterate
to the next one (per the adoption lever model). Organic zero + red canary =
instrument failure → the clock restarts from the fix's deploy; no kill.

Usage:
    python3 scripts/ops/dialectic_canary.py [--url URL] [--log PATH] [--skip-db]

Env:
    UNITARES_MCP_URL              default http://127.0.0.1:8767/mcp/
    UNITARES_MCP_BEARER_TOKEN     optional credential for a gated /mcp endpoint
    UNITARES_DIALECTIC_CANARY_LOG default <repo>/data/logs/dialectic_canary.jsonl
    GOVERNANCE_DATABASE_URL       default postgresql://postgres:postgres@localhost:5432/governance
    UNITARES_CANARY_TIMEOUT_S     default 150 (must clear the 105s one-call ceiling)
    UNITARES_CANARY_VERDICT_TIMEOUT_S default 120 (terminal-review wait)
    UNITARES_CANARY_POLL_INTERVAL_S   default 2 (read-only poll cadence)
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
from typing import Any, Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_LOG = REPO_ROOT / "data" / "logs" / "dialectic_canary.jsonl"
DEFAULT_URL = os.environ.get("UNITARES_MCP_URL", "http://127.0.0.1:8767/mcp/")
CANARY_NAME = "canary_dialectic"
LABEL_PREFIX = "canary_"
TERMINAL_PHASES = {"resolved", "failed", "escalated", "timeout", "abandoned"}


def _timeout_s() -> float:
    raw = os.environ.get("UNITARES_CANARY_TIMEOUT_S")
    try:
        return float(raw) if raw else 150.0
    except ValueError:
        return 150.0


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def _verdict_timeout_s() -> float:
    return _positive_float_env("UNITARES_CANARY_VERDICT_TIMEOUT_S", 120.0)


def _poll_interval_s() -> float:
    return _positive_float_env("UNITARES_CANARY_POLL_INTERVAL_S", 2.0)


async def call_tool(
    url: str, tool_name: str, arguments: Dict[str, Any], timeout_s: float
) -> Dict[str, Any]:
    """One streamable-HTTP tool call, fresh transport per call.

    Deliberately NOT scripts/ops/mcp_agent.py: its client pins a 30s httpx
    timeout, which would kill a legitimate one-call review (ceiling 105s,
    #1442) and turn every canary run into a false red. Identity continuity
    across the two calls rides on client_session_id, not the transport.
    """
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from src.mcp_compat import mcp_bearer_headers, mcp_httpx

    # mcp 2.x's transport calls .sse() on the injected client and is written
    # against httpx2; mcp_httpx() hands back whichever library this mcp wants.
    http_client = mcp_httpx().AsyncClient(
        timeout=timeout_s, headers=mcp_bearer_headers()
    )
    async with streamable_http_client(url, http_client=http_client) as streams:
        # mcp 1.x yields (read, write, get_session_id); 2.x drops the third.
        read, write = streams[0], streams[1]
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


def evaluate_terminal_review(
    payload: Dict[str, Any],
) -> Tuple[bool, bool, str]:
    """Return ``(terminal, ok, detail)`` for a dialectic session read.

    A non-terminal phase tells the poller to keep waiting. Any response error
    or terminal phase other than ``resolved`` is a completed red result. A
    resolved row must carry an action in its resolution payload; otherwise the
    row says terminal without preserving the verdict this canary is meant to
    prove.
    """
    if payload.get("success") is not True:
        return True, False, f"dialectic get failed: {payload.get('error', payload)}"
    raw = review_payload(payload)
    if raw.get("success") is not True:
        return True, False, f"dialectic get failed: {raw.get('error', raw)}"

    phase = str(raw.get("phase") or raw.get("status") or "").strip().lower()
    if phase not in TERMINAL_PHASES:
        return False, False, f"review still in phase {phase or 'unknown'}"
    if phase != "resolved":
        return True, False, f"review ended in terminal phase {phase!r}, not 'resolved'"

    resolution = raw.get("resolution")
    action = resolution.get("action") if isinstance(resolution, dict) else None
    if not action:
        return True, False, "resolved review carries no resolution action"
    return True, True, "ok"


async def wait_for_terminal_review(
    *,
    url: str,
    session_id: str,
    client_session_id: str,
    initial_payload: Dict[str, Any],
    timeout_s: float,
    poll_interval_s: float,
) -> Tuple[Dict[str, Any], bool, str, int]:
    """Poll the read-only session view until a terminal verdict or deadline."""
    payload = initial_payload
    polls = 0
    deadline = time.monotonic() + timeout_s
    while True:
        terminal, ok, detail = evaluate_terminal_review(payload)
        if terminal:
            return payload, ok, detail, polls

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raw = review_payload(payload)
            phase = raw.get("phase") or raw.get("status") or "unknown"
            return (
                payload,
                False,
                f"review did not reach a terminal verdict within {timeout_s:.1f}s "
                f"(last phase {phase!r})",
                polls,
            )

        await asyncio.sleep(min(poll_interval_s, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        payload = await call_tool(
            url,
            "dialectic",
            {
                "action": "get",
                "session_id": session_id,
                "client_session_id": client_session_id,
            },
            timeout_s=min(15.0, max(1.0, remaining)),
        )
        polls += 1


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
                    "surface end-to-end."
                ),
                "reasoning": (
                    "Daily liveness probe. Establishes that this exact call "
                    "path works, so that any reading of organic usage "
                    "counts is not silently measuring broken plumbing."
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

        record["stage"] = "await_terminal_review"
        terminal_payload, ok, detail, polls = await wait_for_terminal_review(
            url=url,
            session_id=raw_review["session_id"],
            client_session_id=csid,
            initial_payload=review,
            timeout_s=_verdict_timeout_s(),
            poll_interval_s=_poll_interval_s(),
        )
        terminal = review_payload(terminal_payload)
        resolution = terminal.get("resolution")
        record["terminal_phase"] = terminal.get("phase") or terminal.get("status")
        record["review_verdict"] = (
            resolution.get("action")
            if isinstance(resolution, dict)
            else terminal.get("review_verdict")
        )
        record["whose_move"] = terminal.get("whose_move")
        record["terminal_poll_count"] = polls
        record["terminal_latency_s"] = round(time.monotonic() - t0, 2)
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
