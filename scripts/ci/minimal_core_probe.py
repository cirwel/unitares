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
]


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
    """The error a tool result reports, or None when it succeeded.

    A strict-identity refusal is success-shaped (no ``success: false``), so it
    is detected by its own markers, the rule identity_refusal_status() uses.
    """
    if first(result, "rollout_flag") == "STRICT_IDENTITY_REQUIRED" or first(result, "refused") is True:
        return "refused: " + str(first(result, "status") or "identity_required")
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
    # Only 200/503 with a real report count; a 401 or an error page is not health.
    if code not in (200, 503) or body.get("status") not in ("healthy", "moderate", "critical") or not checks:
        return {"ok": False, "error": f"not a health report: HTTP {code}, status {body.get('status')!r}"}
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
    extensions = q("select string_agg(extname||'='||extversion, ',' order by extname) from pg_extension")
    migrations = q("select count(*)||' applied, max '||coalesce(max(version),0) from core.schema_migrations")
    errors = [v for v in (extensions, migrations) if v.startswith("error:")]
    return {"ok": not errors, "error": "; ".join(errors) or None,
            "extensions": extensions, "migrations": migrations}


def onboard(name: str) -> tuple[str | None, str | None, dict]:
    result = call("onboard", {"name": name, "force_new": True})
    return first(result, "uuid"), first(result, "client_session_id"), result


def resolved_as(metrics: dict, uuid: str, when: str) -> str | None:
    got = first(metrics, "agent_uuid")
    if got == uuid:
        return None
    return f"{when}: session id did not bind (unbound)" if got is None else f"{when}: resolved a different agent"


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
    error = failed(checkin) or failed(metrics) or resolved_as(metrics, uuid, "second call")
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
    # An unbound low-severity store falls back to an anonymous writer; that is
    # an attribution failure even though the store succeeds.
    if first(stored, "agent_mode") == "anonymous":
        return {"ok": False, "error": "store fell back to an anonymous writer"}
    found = call("knowledge", {"action": "search", "query": "matrix knowledge probe"})
    hit = marker in json.dumps(found)
    return {"ok": hit and not failed(found), "error": failed(found) or (None if hit else "stored note not found"),
            "search_mode": first(found, "search_mode_used") or first(found, "search_mode")}


def dialectic_flow(state: dict) -> dict:
    _, csid, result = onboard("matrix-dialectic")
    if not csid:
        return {"ok": False, "error": failed(result) or "no client_session_id"}
    r = call("dialectic", {"action": "request", "client_session_id": csid,
                           "reason": "matrix dialectic probe", "issue_description": "matrix dialectic probe"})
    error = failed(r) or (None if first(r, "session_id") else
                          f"no session_id in response ({first(r, 'reason') or first(r, 'message') or 'no reason'})")
    return {"ok": error is None, "error": error, "awaiting_facilitation": first(r, "awaiting_facilitation")}


def coordination_demo() -> dict:
    out = subprocess.run([sys.executable, "scripts/demo/coordination_demo.py"],
                         capture_output=True, text=True, timeout=180)
    tail = ((out.stdout + out.stderr).strip().splitlines()[-1:] or [""])[0][:200]
    return {"ok": out.returncode == 0, "rc": out.returncode, "error": None if out.returncode == 0 else tail}


def lineage_graph(state: dict, compose: list[str]) -> dict:
    """Onboard a child with declared lineage; the server writes the AGE SPAWNED
    edge in the background and logs which way it went."""
    if not state.get("uuid"):
        return {"ok": False, "error": "no identity"}
    r = call("onboard", {"name": "matrix-child", "force_new": True,
                         "parent_agent_id": state["uuid"], "spawn_reason": "subagent"})
    if failed(r) or not first(r, "uuid"):
        return {"ok": False, "error": "child onboard: " + (failed(r) or "no uuid")}
    for _ in range(15):
        text = service_logs(compose, "governance-mcp")
        if "SPAWNED edge creation failed" in text:
            return {"ok": False, "error": "SPAWNED edge creation failed (graph write)"}
        if "[SPAWNED] Created edge" in text:
            return {"ok": True}
        time.sleep(2)
    return {"ok": False, "error": "no SPAWNED log line within 30s"}


def service_logs(compose: list[str], service: str) -> str:
    out = subprocess.run([*compose, "logs", "--no-color", service], capture_output=True, text=True, timeout=60)
    return out.stdout + out.stderr


def containers(compose: list[str]) -> dict:
    ids = subprocess.run([*compose, "ps", "-aq"], capture_output=True, text=True, timeout=60).stdout.split()
    if not ids:
        return {}
    out = subprocess.run(["docker", "inspect", "-f",
                          "{{.Name}} {{.RestartCount}} {{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}", *ids],
                         capture_output=True, text=True, timeout=60).stdout
    rows = {}
    for line in out.splitlines():
        name, restarts, status, code, oom = line.split()
        rows[name.lstrip("/")] = {"restarts": int(restarts), "status": status, "exit": int(code), "oom": oom == "true"}
    return rows


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
    error = failed(metrics) or failed(checkin) or resolved_as(metrics, state["uuid"], "after restart")
    return {"ok": error is None, "error": error}


def log_signatures(compose: list[str]) -> dict:
    text = service_logs(compose, "governance-mcp")
    pg = service_logs(compose, "postgres-age")
    return {"ok": True, "counts": {sig: text.count(sig) for sig in LOG_SIGNATURES},
            "postgres_init_complete": "UNITARES init: complete" in pg,
            "postgres_errors": pg.count("ERROR:")}


def run(config: str, out: Path, compose: list[str], up_rc: int) -> None:
    results: dict = {"config": config, "compose_up_rc": up_rc, "complete": False}
    state = {"config": config}
    out.parent.mkdir(parents=True, exist_ok=True)

    def flush() -> None:  # after every probe, so a killed leg still leaves a row
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(results, indent=2, sort_keys=True))
        os.replace(tmp, out)

    steps = [("containers_start", lambda: containers(compose)),
             ("ready", lambda: wait_ready(240)),
             ("database", database)]
    for name, fn in steps:
        probe(name, results, fn)
        flush()
    if results["ready"].get("ok"):
        for name, fn in [("deep_health", deep_health),
                         ("identity", lambda: identity_flow(state)),
                         ("knowledge", lambda: knowledge_flow(state)),
                         ("lineage_graph", lambda: lineage_graph(state, compose)),
                         ("dialectic", lambda: dialectic_flow(state)),
                         ("coordination_demo", coordination_demo),
                         ("logs_before_restart", lambda: log_signatures(compose)),
                         ("containers_before_restart", lambda: containers(compose)),
                         ("restart_resume", lambda: restart_resume(state, compose))]:
            probe(name, results, fn)
            flush()
    for name, fn in [("logs", lambda: log_signatures(compose)), ("containers_end", lambda: containers(compose))]:
        probe(name, results, fn)
    results["complete"] = True
    flush()
    print(json.dumps(results, indent=2, sort_keys=True))


COLUMNS = ["ready", "identity", "knowledge", "lineage_graph", "dialectic", "coordination_demo", "restart_resume"]


def cell(r: dict | None) -> str:
    if not r:
        return "–"
    return "✅" if r.get("ok") else "❌"


def crash_restarts(r: dict) -> str:
    """Restarts Docker made on its own for governance-mcp over the whole leg,
    startup included. The probe's manual restart resets RestartCount to 0, so
    the count before it and the count after it are added."""
    def count(phase: str) -> int | None:
        row = (r.get(phase) or {}).get("unitares-governance-mcp")
        return None if row is None else int(row.get("restarts", 0))
    before, end = count("containers_before_restart"), count("containers_end")
    if before is not None:
        return str(before + (end or 0))
    for value in (end, count("containers_start")):
        if value is not None:
            return str(value)
    return "–"


def summarize(directory: Path, expect: list[str] | None = None) -> str:
    rows = [json.loads(p.read_text()) for p in sorted(directory.glob("*/*.json")) + sorted(directory.glob("*.json"))]
    seen = {r["config"] for r in rows}
    rows += [{"config": name, "compose_up_rc": "no result"} for name in (expect or []) if name not in seen]
    lines = ["| config | up rc | " + " | ".join(COLUMNS)
             + " | deep health | failing | degraded | continuity / redis / lease plane | server crash restarts | migrations |",
             "|---|---|" + "---|" * len(COLUMNS) + "---|---|---|---|---|---|"]
    notes = []
    for r in rows:
        deep = r.get("deep_health") or {}
        checks = deep.get("checks") or {}
        lines.append("| " + " | ".join([
            r["config"], str(r.get("compose_up_rc")), *[cell(r.get(c)) for c in COLUMNS],
            str(deep.get("status", "–")), ", ".join(deep.get("failing") or []) or "–",
            ", ".join(deep.get("degraded") or []) or "–",
            " / ".join(str(x or "–") for x in (deep.get("identity_continuity_mode"),
                                               checks.get("redis_cache"), checks.get("lease_plane"))) if deep else "–",
            crash_restarts(r),
            str((r.get("database") or {}).get("migrations", "–")),
        ]) + " |")
        if r.get("complete") is False:
            notes.append(f"- **{r['config']}** run did not complete; later probes are missing")
        for c in COLUMNS + ["deep_health", "database"]:
            v = r.get(c) or {}
            if v and not v.get("ok"):
                why = v.get("error") or json.dumps({k: x for k, x in v.items() if k != "ok"})[:200]
                notes.append(f"- **{r['config']}** {c}: {why}")
        if deep.get("primary_db_missing"):
            notes.append(f"- **{r['config']}** primary_db missing extensions: {deep['primary_db_missing']}")
        logs = r.get("logs") or {}
        hits = {k: n for k, n in (logs.get("counts") or {}).items() if n}
        if hits:
            notes.append(f"- **{r['config']}** log signatures (whole run, includes one probe restart): {hits}")
        if logs and not logs.get("postgres_init_complete"):
            notes.append(f"- **{r['config']}** postgres init did not log 'UNITARES init: complete'"
                         f" ({logs.get('postgres_errors')} ERROR lines)")
        if (r.get("database") or {}).get("extensions"):
            notes.append(f"- **{r['config']}** extensions: {r['database']['extensions']}")
    return "\n".join(lines + ["", "### Details", ""] + notes) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--up-rc", type=int, default=0)
    parser.add_argument("--summarize", type=Path)
    parser.add_argument("--expect", default="", help="comma-separated configs that should have a result")
    parser.add_argument("compose", nargs="*", help="compose command and files, after --")
    args = parser.parse_args()
    if args.summarize:
        sys.stdout.write(summarize(args.summarize, [c for c in args.expect.split(",") if c]))
        return 0
    if not (args.config and args.out and args.compose):
        parser.error("--config, --out and the compose command are required")
    run(args.config, args.out, args.compose, args.up_rc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
