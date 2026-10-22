#!/usr/bin/env python3
"""Probe a running Compose stack and record what works in one configuration.

This is a measurement, not a gate: every probe records its outcome and the
script exits 0 unless the harness itself breaks. `.github/workflows/
minimal-core-matrix.yml` runs it once per configuration (Redis off, lease
plane off, AGE off, ...) and `--summarize` turns the per-configuration JSON
files into one table. Stdlib only, so it runs on a stock CI runner.

Usage:
    minimal_core_probe.py --config NAME --out results/NAME.json -- <compose args>
    minimal_core_probe.py --summarize results/ > summary.md
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("UNITARES_PROBE_URL", "http://127.0.0.1:8767")
PG = ["docker", "exec", "unitares-postgres-age", "psql", "-U", "postgres", "-d", "governance", "-tAc"]
LOG_SIGNATURES = [
    "degraded-local",
    "Redis connection failed",
    "missing required extension(s)",
    "LOAD 'age' failed",
    "Cypher query failed",
    "Rate limit exceeded",
]
BUDGET_CALLS = 21  # the per-agent KG store budget is 20 new discoveries per hour


def http(path: str, body: dict | None = None, timeout: float = 60) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, code = resp.read(), resp.status
    except urllib.error.HTTPError as exc:  # a 503 deep-health answer still carries JSON
        raw, code = exc.read(), exc.code
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw.decode(errors="replace")[:500]


def call(tool: str, args: dict) -> dict:
    code, body = http("/v1/tools/call", {"name": tool, "arguments": args})
    if isinstance(body, dict) and isinstance(body.get("result"), dict):
        return body["result"]
    return {"_http": code, "_raw": body}


def first(obj, key: str):
    """First value for `key` anywhere in a nested response, else None."""
    if isinstance(obj, dict):
        if obj.get(key) is not None:
            return obj[key]
        values = obj.values()
    elif isinstance(obj, list):
        values = obj
    else:
        return None
    for value in values:
        found = first(value, key)
        if found is not None:
            return found
    return None


def failed(result: dict) -> str | None:
    """The error a tool result reports, or None when it succeeded."""
    if result.get("success") is False or "_http" in result:
        return str(first(result, "error_code") or first(result, "error") or result.get("_raw") or "failed")[:200]
    return None


def probe(name: str, results: dict, fn) -> None:
    try:
        results[name] = fn()
    except Exception as exc:  # record, never abort the run
        results[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def wait_ready(seconds: int) -> dict:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        try:
            code, _ = http("/health/ready", timeout=5)
            if code == 200:
                return {"ok": True, "seconds": round(time.monotonic() - start, 1)}
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return {"ok": False, "error": f"not ready after {seconds}s"}


def deep_health() -> dict:
    body: dict | str = {}
    code = 0
    for _ in range(30):
        try:
            code, body = http("/health/deep", timeout=20)
        except (urllib.error.URLError, OSError) as exc:
            body = str(exc)
        if isinstance(body, dict) and body.get("status") not in (None, "unavailable"):
            break
        time.sleep(3)
    if not isinstance(body, dict):
        return {"ok": False, "error": str(body)[:200]}
    checks = body.get("checks") or {}
    summary = body.get("operator_summary") or {}
    return {
        "ok": True,
        "http": code,
        "status": body.get("status"),
        "checks": {k: v.get("status") for k, v in checks.items() if isinstance(v, dict)},
        "degraded": summary.get("degraded_checks") or [],
        "failing": summary.get("failing_checks") or [],
        "lease_plane": checks.get("lease_plane"),
        "primary_db_missing": first(checks.get("primary_db"), "missing_extensions"),
        "identity_continuity_mode": first(body, "identity_continuity_mode"),
    }


def database() -> dict:
    def q(sql: str) -> str:
        out = subprocess.run([*PG, sql], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else f"error: {out.stderr.strip()[:200]}"
    return {
        "ok": True,
        "extensions": q("select string_agg(extname||'='||extversion, ',' order by extname) from pg_extension"),
        "migrations": q("select count(*)||' applied, max '||coalesce(max(version),0) from core.schema_migrations"),
    }


def onboard(name: str) -> tuple[str | None, str | None, dict]:
    result = call("onboard", {"name": name, "force_new": True})
    return first(result, "uuid"), first(result, "client_session_id"), result


def identity_flow(state: dict) -> dict:
    uuid, csid, result = onboard("matrix-identity")
    if not (uuid and csid):
        return {"ok": False, "error": failed(result) or "no uuid/client_session_id"}
    state.update(uuid=uuid, csid=csid)
    checkin = call("process_agent_update", {
        "client_session_id": csid, "response_text": "matrix probe check-in",
        "complexity": 0.3, "confidence": 0.7, "response_mode": "compact",
    })
    metrics = call("get_governance_metrics", {"client_session_id": csid})
    same = first(metrics, "agent_uuid") == uuid
    error = failed(checkin) or failed(metrics) or (None if same else "second call resolved a different agent")
    return {"ok": error is None, "error": error, "checkin_action": first(checkin, "action")}


def knowledge_flow(state: dict) -> dict:
    csid = state.get("csid")
    if not csid:
        return {"ok": False, "error": "no identity"}
    marker = f"matrix knowledge probe {state['config']}"
    stored = call("knowledge", {"action": "store", "client_session_id": csid, "summary": marker,
                                "discovery_type": "note", "tags": ["ephemeral", "ci-matrix"]})
    if failed(stored):
        return {"ok": False, "error": "store: " + failed(stored)}
    found = call("knowledge", {"action": "search", "query": "matrix knowledge probe"})
    hit = marker in json.dumps(found)
    return {"ok": hit and not failed(found), "error": failed(found) or (None if hit else "stored note not found"),
            "search_mode": first(found, "search_mode_used") or first(found, "search_mode")}


def store_budget(state: dict) -> dict:
    _, csid, result = onboard("matrix-budget")
    if not csid:
        return {"ok": False, "error": failed(result) or "no client_session_id"}
    accepted, refusal = 0, None
    for i in range(BUDGET_CALLS):
        r = call("knowledge", {"action": "store", "client_session_id": csid,
                               "summary": f"matrix budget probe {state['config']} {i}",
                               "discovery_type": "note", "tags": ["ephemeral"]})
        if failed(r):
            refusal = refusal or failed(r)
        else:
            accepted += 1
    # ok means the limit held: the 21st store in an hour is refused.
    return {"ok": accepted <= BUDGET_CALLS - 1, "accepted": accepted, "of": BUDGET_CALLS, "refusal": refusal}


def dialectic_flow(state: dict) -> dict:
    _, csid, result = onboard("matrix-dialectic")
    if not csid:
        return {"ok": False, "error": failed(result) or "no client_session_id"}
    r = call("dialectic", {"action": "request", "client_session_id": csid,
                           "reason": "matrix dialectic probe", "issue_description": "matrix dialectic probe"})
    return {"ok": not failed(r) and bool(first(r, "session_id")), "error": failed(r),
            "awaiting_facilitation": first(r, "awaiting_facilitation")}


def coordination_demo() -> dict:
    out = subprocess.run([sys.executable, "scripts/demo/coordination_demo.py"],
                         capture_output=True, text=True, timeout=180)
    tail = (out.stdout + out.stderr).strip().splitlines()[-1:] or [""]
    return {"ok": out.returncode == 0, "rc": out.returncode, "last_line": tail[0][:200]}


def restart_resume(state: dict, compose: list[str]) -> dict:
    if not state.get("csid"):
        return {"ok": False, "error": "no identity"}
    subprocess.run([*compose, "restart", "governance-mcp"], check=True, capture_output=True, timeout=180)
    ready = wait_ready(240)
    if not ready["ok"]:
        return {"ok": False, "error": "not ready after restart"}
    metrics = call("get_governance_metrics", {"client_session_id": state["csid"]})
    checkin = call("process_agent_update", {"client_session_id": state["csid"], "response_text": "after restart",
                                            "complexity": 0.3, "confidence": 0.7, "response_mode": "compact"})
    same = first(metrics, "agent_uuid") == state["uuid"]
    error = failed(metrics) or failed(checkin) or (None if same else "resolved a different agent after restart")
    return {"ok": error is None, "error": error}


def log_signatures(compose: list[str]) -> dict:
    out = subprocess.run([*compose, "logs", "--no-color", "governance-mcp"], capture_output=True, text=True, timeout=60)
    text = out.stdout + out.stderr
    return {"ok": True, "counts": {sig: text.count(sig) for sig in LOG_SIGNATURES}}


def run(config: str, out: Path, compose: list[str], up_rc: int) -> None:
    results: dict = {"config": config, "compose_up_rc": up_rc}
    state = {"config": config}
    probe("ready", results, lambda: wait_ready(240))
    probe("database", results, database)
    if results["ready"].get("ok"):
        probe("deep_health", results, deep_health)
        probe("identity", results, lambda: identity_flow(state))
        probe("knowledge", results, lambda: knowledge_flow(state))
        probe("store_budget", results, lambda: store_budget(state))
        probe("dialectic", results, lambda: dialectic_flow(state))
        probe("coordination_demo", results, coordination_demo)
        probe("restart_resume", results, lambda: restart_resume(state, compose))
    probe("logs", results, lambda: log_signatures(compose))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(json.dumps(results, indent=2, sort_keys=True))


COLUMNS = ["ready", "identity", "knowledge", "store_budget", "dialectic", "coordination_demo", "restart_resume"]


def cell(r: dict | None) -> str:
    if not r:
        return "–"
    return "✅" if r.get("ok") else "❌"


def summarize(directory: Path) -> str:
    rows = [json.loads(p.read_text()) for p in sorted(directory.glob("*/*.json")) + sorted(directory.glob("*.json"))]
    lines = ["| config | up rc | " + " | ".join(COLUMNS) + " | deep health | degraded | migrations |",
             "|---|---|" + "---|" * len(COLUMNS) + "---|---|---|"]
    notes = []
    for r in rows:
        deep = r.get("deep_health") or {}
        lines.append("| " + " | ".join([
            r["config"], str(r.get("compose_up_rc")), *[cell(r.get(c)) for c in COLUMNS],
            str(deep.get("status", "–")), ", ".join(deep.get("degraded") or []) or "–",
            str((r.get("database") or {}).get("migrations", "–")),
        ]) + " |")
        for c in COLUMNS + ["deep_health", "database"]:
            v = r.get(c) or {}
            if c == "store_budget" and v:
                notes.append(f"- **{r['config']}** store budget: {v.get('accepted')}/{v.get('of')} accepted"
                             f" (refusal: {v.get('refusal')})")
            elif v and not v.get("ok") and v.get("error"):
                notes.append(f"- **{r['config']}** {c}: {v['error']}")
        logs = (r.get("logs") or {}).get("counts") or {}
        hits = {k: n for k, n in logs.items() if n}
        if hits:
            notes.append(f"- **{r['config']}** log signatures: {hits}")
        if (r.get("database") or {}).get("extensions"):
            notes.append(f"- **{r['config']}** extensions: {r['database']['extensions']}")
    return "\n".join(lines + ["", "### Details", ""] + notes) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--up-rc", type=int, default=0)
    parser.add_argument("--summarize", type=Path)
    parser.add_argument("compose", nargs="*", help="compose command and files, after --")
    args = parser.parse_args()
    if args.summarize:
        sys.stdout.write(summarize(args.summarize))
        return 0
    if not (args.config and args.out and args.compose):
        parser.error("--config, --out and the compose command are required")
    run(args.config, args.out, args.compose, args.up_rc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
