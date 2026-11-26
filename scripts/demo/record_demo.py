#!/usr/bin/env python3
"""Live 3-agent shared-record demo over the real UNITARES API.

Runs the README's three-agent scenario end-to-end against a live server:
  1. Agent A records an initial finding ("test_upload_retry appears flaky")
     and exits cleanly via ``agent(action="release_presence")``.
  2. Agent B investigates the real cause (module-scoped fake clock leaking
     time across tests), records a replacement finding, supersedes A's claim,
     checks in with ``sync_state``, grades that check-in via ``record_result``
     with the returned ``prediction_id``, and exits cleanly.
  3. Agent C searches shared memory *before* minting an identity (proving
     reads need no identity), sees both findings with A marked superseded and
     linked to B, then declares explicit lineage from A via
     ``start_session(parent_agent_id=..., spawn_reason="explicit")`` and exits.

Prereq: a governance server reachable at http://127.0.0.1:8767.
Either ``docker compose up -d --wait`` or ``python src/mcp_server.py --port 8767``.

Run::

    make demo-record
    # or: python3 scripts/demo/record_demo.py
    # machine-readable JSON: python3 scripts/demo/record_demo.py --json

Port resolution matches ``scripts/demo/quick_demo.py`` and Docker Compose:
``UNITARES_DEMO_URL`` -> ``UNITARES_DEMO_PORT`` -> ``GOVERNANCE_HOST_PORT``
(shell env) -> ``GOVERNANCE_HOST_PORT`` (checkout ``.env``) -> ``8767``.

All findings are tagged ``demo`` and ``ephemeral`` with non-permanent discovery
types so lifecycle cleanup auto-archives them, and demo identities use the
``record-demo-agent-*`` prefix (with ``do_not_use_for_live_validation: true``
on the graded outcome) so analysis scripts exclude them from live cohorts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _dotenv_values(path: Path | None = None) -> dict[str, str]:
    """Simple KEY=VALUE lines from the checkout's .env, quotes dropped."""
    resolved = path or REPO_ROOT / ".env"
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, NotADirectoryError, OSError):
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _governance_port(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> str:
    """Host-side port of the governance server, resolved as Compose does."""
    env = os.environ if environ is None else environ
    return (
        env.get("UNITARES_DEMO_PORT")
        or env.get("GOVERNANCE_HOST_PORT")
        or _dotenv_values(dotenv_path).get("GOVERNANCE_HOST_PORT")
        or "8767"
    )


def _rest_url(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> str:
    env = os.environ if environ is None else environ
    explicit = env.get("UNITARES_DEMO_URL")
    if explicit:
        return explicit
    return f"http://127.0.0.1:{_governance_port(env, dotenv_path)}/v1/tools/call"


def _dashboard_url(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> str:
    return f"http://localhost:{_governance_port(environ, dotenv_path)}/dashboard"


def _health_url(rest_url: str) -> str:
    parsed = urllib.parse.urlsplit(rest_url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/health/live", "", ""))


def _auth_headers(url: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    token = (env.get("UNITARES_HTTP_API_TOKEN") or "").strip()
    if not token:
        return {}
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"Refusing to send UNITARES_HTTP_API_TOKEN over plain HTTP to non-loopback host {host!r}"
        )
    return {"Authorization": f"Bearer {token}"}


def _http_call(rest_url: str, tool: str, args: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
    body = json.dumps({"name": tool, "arguments": args}).encode("utf-8")
    headers = {"Content-Type": "application/json", **_auth_headers(rest_url)}
    req = urllib.request.Request(rest_url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        return payload["result"]
    if isinstance(payload, dict):
        return payload
    raise TypeError(f"Unexpected REST response for {tool}: {type(payload).__name__}")


def preflight(rest_url: str, *, timeout: float = 3.0) -> None:
    health = _health_url(rest_url)
    try:
        req = urllib.request.Request(health, headers=_auth_headers(health), method="GET")
        with urllib.request.urlopen(req, timeout=timeout):
            return
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        sys.exit(
            f"Governance server not reachable at {health}.\n"
            "Start it first:\n"
            "    docker compose up -d --wait        # bundled stack\n"
            "    # or:\n"
            "    python src/mcp_server.py --port 8767   # bare-metal\n"
            "If you changed the host port, run e.g.:\n"
            "    UNITARES_DEMO_PORT=18767 make demo-record\n"
            f"\nError: {exc}"
        )


def _extract_uuid(result: Mapping[str, Any]) -> str:
    value = result.get("agent_uuid") or result.get("uuid")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise KeyError(f"start_session response missing agent_uuid/uuid: {sorted(result.keys())}")


def _extract_session_id(result: Mapping[str, Any]) -> str:
    value = result.get("client_session_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise KeyError(f"start_session response missing client_session_id: {sorted(result.keys())}")


def _extract_discovery_id(result: Mapping[str, Any]) -> str:
    value = result.get("discovery_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    discovery = result.get("discovery")
    if isinstance(discovery, Mapping) and isinstance(discovery.get("id"), str) and discovery["id"].strip():
        return discovery["id"].strip()
    raise KeyError(f"store_finding response missing discovery_id: {sorted(result.keys())}")


def _extract_search_rows(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("discoveries", "memory_suggestions", "results"):
        rows = result.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _row_id(row: Mapping[str, Any]) -> str | None:
    for key in ("discovery_id", "id"):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _is_error_result(result: Mapping[str, Any]) -> bool:
    if result.get("success") is False:
        return True
    if result.get("error"):
        return True
    return False


def run_record_demo(
    call_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    *,
    run_id: str | None = None,
    dashboard_url: str | None = None,
) -> dict[str, Any]:
    """Execute the 3-agent shared-record scenario using ``call_fn(tool, args)``."""
    rid = run_id or uuid.uuid4().hex[:8]
    run_tag = f"demo-run-{rid}"
    base_tags = ["demo", "ephemeral", "upload-retry", run_tag]

    # ── 1. Agent A: hits failing test, logs flaky claim, exits cleanly ──
    start_a = call_fn(
        "start_session",
        {
            "force_new": True,
            "name": f"record-demo-agent-a-{rid}",
            "model_type": "demo",
        },
    )
    uuid_a = _extract_uuid(start_a)
    sess_a = _extract_session_id(start_a)

    store_a = call_fn(
        "store_finding",
        {
            "client_session_id": sess_a,
            "discovery_type": "bug_found",
            "summary": f"[{rid}] test_upload_retry failed in CI — appears flaky under retry backoff",
            "content": (
                "Observed intermittent failure in test_upload_retry during retry backoff. "
                "Recording initial symptom before handing off investigation."
            ),
            "tags": base_tags,
        },
    )
    finding_a = _extract_discovery_id(store_a)

    release_a = call_fn(
        "agent",
        {
            "action": "release_presence",
            "client_session_id": sess_a,
        },
    )

    # ── 2. Agent B: finds root cause, supersedes A, checks in & grades outcome ──
    start_b = call_fn(
        "start_session",
        {
            "force_new": True,
            "name": f"record-demo-agent-b-{rid}",
            "model_type": "demo",
        },
    )
    uuid_b = _extract_uuid(start_b)
    sess_b = _extract_session_id(start_b)

    store_b = call_fn(
        "store_finding",
        {
            "client_session_id": sess_b,
            "discovery_type": "bug_fix",
            "summary": f"[{rid}] test_upload_retry root cause: module-scoped fake clock leaks time",
            "content": (
                "Not a flaky retry: the fake clock fixture is module-scoped, so earlier "
                "tests advance virtual time past the retry deadline. Switching the fixture "
                "to function scope passed 50 consecutive shuffled runs."
            ),
            "tags": [*base_tags, "clock-skew"],
        },
    )
    finding_b = _extract_discovery_id(store_b)

    supersede_note = (
        f"Superseded by root-cause finding {finding_b}: module-scoped fake clock "
        "leaks virtual time across tests."
    )
    supersede_res = call_fn(
        "knowledge",
        {
            "action": "supersede",
            "client_session_id": sess_b,
            "discovery_id": finding_b,
            "supersedes_id": finding_a,
            "resolution_notes": supersede_note,
        },
    )
    supersede_mode = "age_supersedes_edge"
    if _is_error_result(supersede_res):
        supersede_mode = "relational_status_fallback"
        supersede_res = call_fn(
            "update_finding",
            {
                "client_session_id": sess_b,
                "discovery_id": finding_a,
                "status": "superseded",
                "superseded_by": finding_b,
                "resolution_notes": supersede_note,
            },
        )

    checkin_b = call_fn(
        "sync_state",
        {
            "client_session_id": sess_b,
            "response_text": (
                "Found module-scoped fake clock root cause for test_upload_retry, "
                "superseded the initial flaky-test claim, and switched the fixture "
                "to function scope."
            ),
            "complexity": 0.45,
            "confidence": 0.85,
            "task_type": "bugfix",
        },
    )
    prediction_id = checkin_b.get("prediction_id")
    action_summary = checkin_b.get("action_summary") if isinstance(checkin_b.get("action_summary"), dict) else {}

    outcome_args: dict[str, Any] = {
        "client_session_id": sess_b,
        "outcome_type": "test_passed",
        "outcome_score": 1.0,
        "detail": {
            "test_name": "test_upload_retry",
            "runs": 50,
            "order": "shuffled",
            "demo_run": rid,
            "do_not_use_for_live_validation": True,
        },
    }
    if isinstance(prediction_id, str) and prediction_id.strip():
        outcome_args["prediction_id"] = prediction_id.strip()

    outcome_b = call_fn("record_result", outcome_args)
    outcome_summary = outcome_b.get("state_summary") if isinstance(outcome_b.get("state_summary"), dict) else {}

    release_b = call_fn(
        "agent",
        {
            "action": "release_presence",
            "client_session_id": sess_b,
        },
    )

    # ── 3. Agent C: searches without identity, then declares explicit lineage from A ──
    search_c = call_fn(
        "search_shared_memory",
        {
            "query": "test_upload_retry",
            "tags": [run_tag],
            "limit": 10,
        },
    )
    rows = _extract_search_rows(search_c)
    by_id = {_row_id(r): r for r in rows if _row_id(r)}
    row_a = by_id.get(finding_a)
    row_b = by_id.get(finding_b)

    start_c = call_fn(
        "start_session",
        {
            "force_new": True,
            "name": f"record-demo-agent-c-{rid}",
            "model_type": "demo",
            "parent_agent_id": uuid_a,
            "spawn_reason": "explicit",
        },
    )
    uuid_c = _extract_uuid(start_c)
    sess_c = _extract_session_id(start_c)
    c_state = start_c.get("state_summary") if isinstance(start_c.get("state_summary"), dict) else {}

    release_c = call_fn(
        "agent",
        {
            "action": "release_presence",
            "client_session_id": sess_c,
        },
    )

    superseded_link = row_a.get("superseded_by") if isinstance(row_a, dict) else None
    checks = {
        "agent_a_released": bool(release_a.get("released", False)),
        "agent_b_released": bool(release_b.get("released", False)),
        "agent_c_released": bool(release_c.get("released", False)),
        "search_found_initial_claim": row_a is not None,
        "search_found_replacement_claim": row_b is not None,
        "initial_claim_marked_superseded": isinstance(row_a, dict) and row_a.get("status") == "superseded",
        "initial_claim_links_replacement": superseded_link == finding_b,
        "outcome_bound_to_prediction": bool(prediction_id) and outcome_summary.get("prediction_binding") == "registry",
        "lineage_points_to_agent_a": c_state.get("predecessor_uuid") == uuid_a,
    }

    return {
        "run_id": rid,
        "run_tag": run_tag,
        "dashboard_url": dashboard_url or _dashboard_url(),
        "agent_a": {
            "uuid": uuid_a,
            "finding_id": finding_a,
            "released": checks["agent_a_released"],
        },
        "agent_b": {
            "uuid": uuid_b,
            "finding_id": finding_b,
            "supersede_mode": supersede_mode,
            "supersede_ok": not _is_error_result(supersede_res),
            "action": action_summary.get("action") or checkin_b.get("action"),
            "verdict_confidence": action_summary.get("verdict_confidence"),
            "prediction_id": prediction_id,
            "prediction_binding": outcome_summary.get("prediction_binding"),
            "corroboration_grade": outcome_summary.get("corroboration_grade"),
            "released": checks["agent_b_released"],
        },
        "agent_c": {
            "uuid": uuid_c,
            "predecessor_uuid": c_state.get("predecessor_uuid"),
            "lineage_state": c_state.get("lineage_state"),
            "search_count": len(rows),
            "initial_status_seen": row_a.get("status") if isinstance(row_a, dict) else None,
            "superseded_by_seen": superseded_link,
            "replacement_status_seen": row_b.get("status") if isinstance(row_b, dict) else None,
            "released": checks["agent_c_released"],
        },
        "checks": checks,
    }


def _banner(title: str) -> str:
    return f"\n{'─' * 68}\n  {title}\n{'─' * 68}"


def format_report(summary: Mapping[str, Any]) -> str:
    a = summary["agent_a"]
    b = summary["agent_b"]
    c = summary["agent_c"]
    checks = summary["checks"]

    lines = [
        _banner(f"UNITARES 3-agent shared-record demo (run {summary['run_id']})"),
        "",
        "1. Agent A — records initial claim and exits cleanly",
        f"   agent_uuid   = {a['uuid']}",
        f"   finding_a    = {a['finding_id']}  (bug_found: test_upload_retry appears flaky)",
        f"   presence     = {'released' if a['released'] else 'not released'}",
        "",
        "2. Agent B — finds root cause, supersedes A, checks in & grades outcome",
        f"   agent_uuid   = {b['uuid']}",
        f"   finding_b    = {b['finding_id']}  (bug_fix: module-scoped fake clock leaks time)",
        f"   supersede    = {b['supersede_mode']} ({'ok' if b['supersede_ok'] else 'failed'})",
        f"   sync_state   = action={b.get('action') or '-'} ({b.get('verdict_confidence') or '-'})",
        f"   prediction   = {b.get('prediction_id') or '-'}",
        f"   record_result= binding={b.get('prediction_binding') or '-'}, grade={b.get('corroboration_grade') or '-'}",
        "",
        "3. Agent C — searches without identity first, then declares lineage from A",
        f"   unbound search ({summary['run_tag']}): {c['search_count']} finding(s) returned",
        f"     • finding_a status={c.get('initial_status_seen') or '-'}  superseded_by={c.get('superseded_by_seen') or '-'}",
        f"     • finding_b status={c.get('replacement_status_seen') or '-'}",
        f"   agent_uuid   = {c['uuid']}",
        f"   lineage      = predecessor={c.get('predecessor_uuid') or '-'}  state={c.get('lineage_state') or '-'}",
        _banner("Verification"),
    ]
    for label, ok in checks.items():
        mark = "OK  " if ok else "WARN"
        lines.append(f"  [{mark}] {label}")
    lines.extend(
        [
            "",
            f"  • Open {summary['dashboard_url']} to inspect the recorded identities, findings, and outcome.",
            "  • Findings are tagged `demo` + `ephemeral` for automatic lifecycle archival.",
            "  • See docs/guides/AGENT_SESSION_WALKTHROUGH.md for captured single-call examples.",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the 3-agent shared-record UNITARES demo against a live governance server.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the formatted walkthrough.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Per-call HTTP timeout in seconds (default: 15.0).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rest_url = _rest_url()
    preflight(rest_url)
    summary = run_record_demo(
        lambda tool, tool_args: _http_call(rest_url, tool, tool_args, timeout=args.timeout),
        dashboard_url=_dashboard_url(),
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_report(summary))
    required_ok = (
        summary["checks"]["search_found_initial_claim"]
        and summary["checks"]["search_found_replacement_claim"]
        and summary["checks"]["initial_claim_marked_superseded"]
        and summary["checks"]["lineage_points_to_agent_a"]
    )
    return 0 if required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
