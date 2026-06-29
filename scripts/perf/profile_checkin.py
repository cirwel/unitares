#!/usr/bin/env python3
"""Hot-path profiler for ``process_agent_update`` — tool dispatch + EISV assessment.

The "testing gaps" review asked for a repeatable profile of the check-in hot path
so the anyio/asyncpg amplification documented in CLAUDE.md ("~60x in-handler") is
*measurable*, not folklore. This harness drives a real check-in sequence on a
realistic payload through the live in-handler path and reports two things:

  1. **In-handler latency** (client-measured wall clock per REST call):
     mean / p50 / p90 / p95 / p99 / max. This is the real path — the handler runs
     inside the MCP server's anyio task group with asyncpg/Redis underneath, which
     is where the amplification lives. (An in-process direct call does NOT
     reproduce it — it short-circuits to the standalone floor — so we drive the
     real surface instead.)

  2. **Per-stage breakdown**, by surfacing the per-phase instrumentation the
     server *already* emits. ``run_process_update_workflow`` logs one
     ``[checkin_phases] total=…ms resolve_identity=…ms … enrichment=…ms …`` INFO
     line per check-in. This harness captures the lines emitted during its own run
     window and aggregates every phase — so the amplification is attributable to a
     stage (in practice ``enrichment`` and ``locked_update`` dominate), not just a
     lump total.

This is READ-ONLY: it adds no production instrumentation and changes no server
code. It only *drives* real check-ins and *reads* the log the server already
writes. It extends ``scripts/dev/parse_update_phase_logs.py`` (which parses only
``total`` + ``enrichment`` from an arbitrary log slice) by generating a controlled
run and decomposing every phase.

Usage::

    # against a running stack on :8767, point --log-file at the server's log
    # (where INFO lines land — typically the stderr log for a launchd service):
    python3 scripts/perf/profile_checkin.py --n 30 \
        --log-file data/logs/mcp_server_error.log

    python3 scripts/perf/profile_checkin.py --n 50 --json   # machine-readable

If the log is not given/readable the harness still reports the in-handler latency
distribution and tells you where the per-stage data lives.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# A realistic check-in payload: a non-trivial work description plus self-report,
# the shape an agent loop actually emits (mirrors scripts/demo/quick_demo.py).
REALISTIC_PAYLOAD = {
    "response_text": (
        "Reworked the session-pool acquisition path under contention; added three "
        "invariants, reworked the lease ladder, and shimmed the asyncpg cursor wrap. "
        "Tests pass; rolled out behind a flag."
    ),
    "complexity": 0.45,
    "confidence": 0.8,
    "task_type": "convergent",
}

PHASE_LINE_MARKER = "[checkin_phases]"
# The enrichment stage logs its own per-enricher breakdown. `=skip` entries are
# non-numeric and naturally excluded by the ms regex.
ENRICHMENT_LINE_MARKER = "[enrichment_phases]"


def _default_rest_url() -> str:
    explicit = os.environ.get("UNITARES_DEMO_URL")
    if explicit:
        return explicit
    port = (
        os.environ.get("UNITARES_DEMO_PORT")
        or os.environ.get("GOVERNANCE_HOST_PORT")
        or "8767"
    )
    return f"http://127.0.0.1:{port}/v1/tools/call"


def _call(url: str, tool: str, args: dict, timeout: float = 30.0) -> dict:
    body = json.dumps({"name": tool, "arguments": args}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["result"]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * pct)]


def _dist(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 1),
        "p50": round(_percentile(values, 0.50), 1),
        "p90": round(_percentile(values, 0.90), 1),
        "p95": round(_percentile(values, 0.95), 1),
        "p99": round(_percentile(values, 0.99), 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
    }


def _parse_phase_line(line: str) -> dict[str, float]:
    """Extract every ``<phase>=<n>ms`` pair from one [checkin_phases] line."""
    return {key: float(val) for key, val in re.findall(r"(\w+)=(\d+(?:\.\d+)?)ms", line)}


def _read_new_phase_lines(
    log_path: Path, start_offset: int
) -> tuple[list[dict[str, float]], list[dict[str, float]], int]:
    """Read [checkin_phases] and [enrichment_phases] lines appended after
    ``start_offset`` bytes."""
    checkin: list[dict[str, float]] = []
    enrichment: list[dict[str, float]] = []
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(start_offset)
        for line in handle:
            if PHASE_LINE_MARKER in line:
                fields = _parse_phase_line(line)
                if fields:
                    checkin.append(fields)
            elif ENRICHMENT_LINE_MARKER in line:
                fields = _parse_phase_line(line)
                if fields:
                    enrichment.append(fields)
        end_offset = handle.tell()
    return checkin, enrichment, end_offset


def _resolve_log_path(arg: str | None) -> Path | None:
    candidates = []
    if arg:
        candidates.append(Path(arg))
    env = os.environ.get("UNITARES_MCP_LOG")
    if env:
        candidates.append(Path(env))
    # Relative fallbacks — INFO usually lands in the stderr log for a service.
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "data" / "logs" / "mcp_server_error.log")
    candidates.append(repo_root / "data" / "logs" / "mcp_server.log")
    for path in candidates:
        if path.is_file():
            return path
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=30, help="number of timed check-ins (default 30)")
    parser.add_argument("--warmup", type=int, default=3, help="warmup check-ins, not timed (default 3)")
    parser.add_argument("--url", default=_default_rest_url(), help="REST tools endpoint")
    parser.add_argument(
        "--log-file",
        default=None,
        help="server log with [checkin_phases] INFO lines (env: UNITARES_MCP_LOG). "
        "Falls back to data/logs/mcp_server_error.log then mcp_server.log.",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # Preflight: a running stack is required.
    try:
        onboard = _call(
            args.url, "onboard",
            {"name": "perf-profile-checkin", "model_type": "resident_agent", "force_new": True},
        )
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        print(f"ERROR: governance server not reachable at {args.url}: {exc}", file=sys.stderr)
        print("Start it (docker compose up -d --wait, or python src/mcp_server.py --port 8767).", file=sys.stderr)
        return 2

    session = onboard["client_session_id"]

    # Locate the phase log and mark our start point so we only read OUR run's lines.
    log_path = _resolve_log_path(args.log_file)
    start_offset = log_path.stat().st_size if log_path else 0

    payload = dict(REALISTIC_PAYLOAD, client_session_id=session, response_mode="compact")

    # Warmup (imports, caches, pools) — not timed.
    for _ in range(max(0, args.warmup)):
        _call(args.url, "process_agent_update", payload)

    # Timed run.
    latencies_ms: list[float] = []
    for _ in range(args.n):
        start = time.perf_counter()
        _call(args.url, "process_agent_update", payload)
        latencies_ms.append((time.perf_counter() - start) * 1000.0)

    latency_dist = _dist(latencies_ms)

    # Per-stage breakdown from the server's own [checkin_phases] instrumentation,
    # plus the per-enricher breakdown of the enrichment stage ([enrichment_phases]).
    stage_report: dict | None = None
    enrichment_report: dict | None = None
    phase_note = None
    if log_path:
        # Give the fire-and-forget log flush a beat to land.
        time.sleep(1.0)
        phase_lines, enrichment_lines, _ = _read_new_phase_lines(log_path, start_offset)
        if phase_lines:
            phases = list(dict.fromkeys(k for fields in phase_lines for k in fields))
            ordered = (["total"] if "total" in phases else []) + [p for p in phases if p != "total"]
            stage_report = {
                "log_file": str(log_path),
                "phase_lines_captured": len(phase_lines),
                "checkins_driven": args.n,
                "stages": {p: _dist([f[p] for f in phase_lines if p in f]) for p in ordered},
            }
        else:
            phase_note = (
                f"no [checkin_phases] lines found in {log_path} for this run — "
                "the server may log elsewhere; pass --log-file or set UNITARES_MCP_LOG."
            )
        if enrichment_lines:
            enrichers = sorted(
                {k for fields in enrichment_lines for k in fields},
                key=lambda k: statistics.mean([f[k] for f in enrichment_lines if k in f]),
                reverse=True,
            )
            # Per-call sum of attributed enricher time vs the enrichment stage
            # wall-clock — a small gap is scheduling overhead; a large dominant
            # enricher means the cost is that enricher's own (KG/DB) await work.
            enrichment_report = {
                "enrichment_lines_captured": len(enrichment_lines),
                "attributed_sum_ms": _dist([sum(f.values()) for f in enrichment_lines]),
                "top_enrichers": {
                    k: _dist([f[k] for f in enrichment_lines if k in f]) for k in enrichers[:8]
                },
            }

    else:
        phase_note = (
            "no readable server log found; per-stage data lives in the server's "
            "[checkin_phases] INFO lines — pass --log-file or set UNITARES_MCP_LOG."
        )

    result = {
        "url": args.url,
        "n": args.n,
        "warmup": args.warmup,
        "in_handler_latency_ms": latency_dist,
        "per_stage": stage_report,
        "enrichment_breakdown": enrichment_report,
        "per_stage_note": phase_note,
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    # Human-readable report.
    print(f"\n{'=' * 68}")
    print(f"  process_agent_update hot-path profile  (n={args.n}, url={args.url})")
    print(f"{'=' * 68}")
    d = latency_dist
    print("\nIn-handler latency (client wall-clock, ms) — the real anyio+asyncpg path:")
    print(f"  mean={d['mean']}  p50={d['p50']}  p90={d['p90']}  p95={d['p95']}  p99={d['p99']}  max={d['max']}")

    if stage_report:
        print(
            f"\nPer-stage breakdown (server [checkin_phases], "
            f"{stage_report['phase_lines_captured']} lines / {args.n} driven):"
        )
        print(f"  {'stage':<18} {'mean':>7} {'p50':>6} {'p95':>6} {'max':>6}")
        for stage, sd in stage_report["stages"].items():
            if sd.get("n"):
                print(f"  {stage:<18} {sd['mean']:>7} {sd['p50']:>6} {sd['p95']:>6} {sd['max']:>6}")
        if stage_report["phase_lines_captured"] != args.n:
            print(
                "  note: captured phase-line count != driven count; concurrent resident "
                "check-ins may share this log. Treat per-stage as fleet-aggregate over the window."
            )
        # Surface the dominant non-total stage — where the amplification concentrates.
        stages = {k: v for k, v in stage_report["stages"].items() if k != "total" and v.get("n")}
        if stages:
            hot = max(stages, key=lambda k: stages[k]["mean"])
            print(
                f"\nDominant stage: '{hot}' (mean={stages[hot]['mean']}ms). Per CLAUDE.md "
                "'Substrate Tax', KG/enrichment work that is ~21-71ms standalone amplifies "
                "in-handler — this stage is where to look."
            )
    elif phase_note:
        print(f"\nPer-stage breakdown: {phase_note}")

    if enrichment_report:
        es = enrichment_report["attributed_sum_ms"]
        print(
            f"\nEnrichment stage breakdown (server [enrichment_phases], "
            f"{enrichment_report['enrichment_lines_captured']} lines):"
        )
        print(f"  attributed enricher sum: mean={es['mean']}ms p95={es['p95']}ms")
        print(f"  {'enricher':<34} {'mean':>7} {'p95':>6} {'max':>6}")
        for name, ed in enrichment_report["top_enrichers"].items():
            if ed.get("n"):
                print(f"  {name:<34} {ed['mean']:>7} {ed['p95']:>6} {ed['max']:>6}")
        print(
            "  note: enrich_mirror_signals (KG semantic search) is the known tail; it skips in\n"
            "  routine compact and is bounded by UNITARES_KG_SEARCH_TIMEOUT_S + cadence + dedup,\n"
            "  so routine traffic pays little. What remains is real but modest, and structural:\n"
            "  the anyio+asyncpg shared-loop amplification (CLAUDE.md 'Substrate Tax') — a bug\n"
            "  class per-process substrates (BEAM/db_connection) lack by construction. On that\n"
            "  architecture axis it is modest supporting evidence for such a substrate; it is\n"
            "  not decided on latency and is not a priority-setter on its own."
        )

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
