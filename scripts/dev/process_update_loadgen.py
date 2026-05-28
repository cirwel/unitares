#!/usr/bin/env python3
"""Drive concurrent process_agent_update calls against a governance MCP server.

This is intentionally a local development/load diagnostic. It mints fresh
short-lived identities, then runs one worker thread per identity so benchmark
traffic exercises multiple agent locks and the post-lock enrichment fan-out.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_URL = "http://127.0.0.1:8767/v1/tools/call"


@dataclass
class ToolCallResult:
    payload: dict[str, Any] | None
    duration_ms: float
    error: str | None


@dataclass
class WorkerResult:
    durations_ms: list[float]
    errors: list[str]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


def _stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {
            "n": 0,
            "min_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": 0.0,
            "mean_ms": 0.0,
        }
    return {
        "n": len(values),
        "min_ms": min(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
        "max_ms": max(values),
        "mean_ms": statistics.mean(values),
    }


def _call_tool(
    *,
    url: str,
    name: str,
    arguments: dict[str, Any],
    agent_header: str,
    session_id: str | None,
    timeout_s: float,
) -> ToolCallResult:
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Id": agent_header,
    }
    if session_id:
        headers["X-Session-ID"] = session_id

    body = json.dumps({"name": name, "arguments": arguments}).encode()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read())
        return ToolCallResult(payload, (time.perf_counter() - started) * 1000, None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        return ToolCallResult(None, (time.perf_counter() - started) * 1000, f"HTTP {exc.code}: {detail}")
    except Exception as exc:  # noqa: BLE001 - load diagnostic should collect all failures.
        return ToolCallResult(None, (time.perf_counter() - started) * 1000, f"{type(exc).__name__}: {exc}")


def _onboard_worker(args: argparse.Namespace, index: int) -> tuple[str, str] | None:
    result = _call_tool(
        url=args.url,
        name="onboard",
        arguments={
            "force_new": True,
            "spawn_reason": args.spawn_reason,
            "name": f"{args.worker_name_prefix}_w{index}",
            "initial_state": {
                "purpose": "process_agent_update_loadgen",
                "worker_index": index,
                "created_by": "scripts/dev/process_update_loadgen.py",
            },
        },
        agent_header=args.agent_header,
        session_id=None,
        timeout_s=args.timeout_s,
    )
    if result.error or result.payload is None:
        print(f"  onboard w{index} ERR: {result.error}", flush=True)
        return None

    body = result.payload.get("result", result.payload)
    agent_uuid = body.get("uuid") or body.get("agent_uuid")
    client_session_id = body.get("client_session_id")
    if not agent_uuid or not client_session_id:
        print(f"  onboard w{index} ERR: missing uuid/client_session_id in {body}", flush=True)
        return None
    return str(agent_uuid), str(client_session_id)


def _update_loop(
    *,
    args: argparse.Namespace,
    label: str,
    client_session_id: str,
    results: dict[str, WorkerResult],
) -> None:
    durations: list[float] = []
    errors: list[str] = []
    for index in range(args.calls_per_worker):
        result = _call_tool(
            url=args.url,
            name="process_agent_update",
            arguments={
                "response_text": (
                    f"{args.payload_label} {label} iter {index}; "
                    "sustained concurrency benchmark for BEAM migration analysis"
                ),
                "complexity": 0.5 + (index % 5) * 0.1,
                "confidence": 0.6 + (index % 4) * 0.08,
                "client_session_id": client_session_id,
                "task_type": "mixed",
                "response_mode": "minimal",
            },
            agent_header=args.agent_header,
            session_id=client_session_id,
            timeout_s=args.timeout_s,
        )
        durations.append(result.duration_ms)
        if result.error:
            errors.append(f"iter {index}: {result.error}")
        elif result.payload is not None:
            body = result.payload.get("result", result.payload)
            if body.get("success") is False:
                errors.append(f"iter {index}: tool returned success=false: {body}")
    results[label] = WorkerResult(durations, errors)


def _print_worker_summary(label: str, result: WorkerResult) -> None:
    stats = _stats(result.durations_ms)
    print(
        f"  [{label}] n={stats['n']} errs={len(result.errors)} "
        f"p50={stats['p50_ms']:.0f}ms p95={stats['p95_ms']:.0f}ms "
        f"p99={stats['p99_ms']:.0f}ms max={stats['max_ms']:.0f}ms",
        flush=True,
    )
    for error in result.errors[:3]:
        print(f"    error: {error}", flush=True)


def run(args: argparse.Namespace) -> dict[str, Any]:
    workers: list[tuple[str, str, str]] = []
    for index in range(args.workers):
        onboarded = _onboard_worker(args, index)
        if onboarded is not None:
            agent_uuid, client_session_id = onboarded
            workers.append((f"w{index}", agent_uuid, client_session_id))

    print(f"onboarded {len(workers)} workers", flush=True)

    results: dict[str, WorkerResult] = {}
    threads: list[threading.Thread] = []
    started = time.perf_counter()
    for label, _agent_uuid, client_session_id in workers:
        thread = threading.Thread(
            target=_update_loop,
            kwargs={
                "args": args,
                "label": label,
                "client_session_id": client_session_id,
                "results": results,
            },
            name=f"loadgen-{label}",
        )
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()
    wall_clock_s = time.perf_counter() - started

    all_durations: list[float] = []
    total_errors = 0
    for label in sorted(results):
        result = results[label]
        _print_worker_summary(label, result)
        all_durations.extend(result.durations_ms)
        total_errors += len(result.errors)

    total_stats = _stats(all_durations)
    print(
        f"TOTAL: {total_stats['n']} calls in {wall_clock_s:.1f}s "
        f"(p50={total_stats['p50_ms']:.0f}ms p95={total_stats['p95_ms']:.0f}ms "
        f"p99={total_stats['p99_ms']:.0f}ms max={total_stats['max_ms']:.0f}ms "
        f"errors={total_errors})",
        flush=True,
    )
    return {
        "url": args.url,
        "workers_requested": args.workers,
        "workers_onboarded": len(workers),
        "calls_per_worker": args.calls_per_worker,
        "wall_clock_s": wall_clock_s,
        "total_errors": total_errors,
        "total": total_stats,
        "workers": {
            label: {
                "errors": result.errors,
                "stats": _stats(result.durations_ms),
            }
            for label, result in sorted(results.items())
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("GOVERNANCE_TOOL_URL", DEFAULT_URL))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("N_WORKERS", "4")))
    parser.add_argument(
        "--calls-per-worker",
        type=int,
        default=int(os.environ.get("N_PER_WORKER", "8")),
    )
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument("--agent-header", default=os.environ.get("LOADGEN_AGENT_HEADER", "process-update-loadgen"))
    parser.add_argument("--worker-name-prefix", default="process_update_loadgen")
    parser.add_argument("--spawn-reason", default="perf_profile_load")
    parser.add_argument("--payload-label", default="process_update_loadgen")
    parser.add_argument("--json-output", help="Optional path for machine-readable benchmark output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    summary = run(args)
    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")
    return 1 if summary["total_errors"] or summary["workers_onboarded"] != args.workers else 0


if __name__ == "__main__":
    raise SystemExit(main())
