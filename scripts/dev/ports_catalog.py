#!/usr/bin/env python3
"""Generate docs/operations/DEFINITIVE_PORTS.md — the canonical service-port registry.

Why a generator and not a hand-written doc: the port table drifted to listing 2
of 5 live ports because each service's port lives in a different place (a bare
constant, a getenv default, a URL-embedded default, a plist template) and nobody
re-types the doc when a service is added. This keeps a single declarative
registry (PORTS) and:

  * renders the doc from it (so the table is always complete), and
  * cross-checks each in-repo port against its cited source file (so the registry
    cannot silently disagree with the code).

Ports that live outside this repo (anima on the Pi) or only in a tracked plist
template are recorded descriptively; everything with an in-repo literal is
verified.

Usage:
    python3 scripts/dev/ports_catalog.py            # write the doc
    python3 scripts/dev/ports_catalog.py --check     # exit 1 if doc stale OR a source lost its port
    python3 scripts/dev/ports_catalog.py --health-probes   # TSV roster for health_watchdog.sh
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "docs" / "operations" / "DEFINITIVE_PORTS.md"


# Single source of truth for service ports. `verify` is the in-repo file whose
# literal must contain the port (None = external / not verifiable from this repo).
PORTS = [
    {
        "port": 8766,
        "service": "Anima MCP (physical edge testbed)",
        "host": "edge host (Raspberry Pi)",
        "source": "`anima-mcp` service config (external repo)",
        "verify": None,
        "health": {"path": "/health", "expect": [200], "scope": "edge"},
    },
    {
        "port": 8767,
        "service": "UNITARES governance MCP",
        "host": "governance host",
        "source": "`src/mcp_server.py` — `DEFAULT_PORT`",
        "verify": "src/mcp_server.py",
        "health": {"path": "/health", "expect": [200], "scope": "governance"},
    },
    {
        "port": 8768,
        "service": "Gateway (weak external tier only)",
        "host": "governance host",
        "source": "`src/gateway/constants.py` — `GATEWAY_PORT` (`GATEWAY_PORT` env, default 8768)",
        "verify": "src/gateway/constants.py",
        "health": {"path": "/health", "expect": [200], "scope": "governance"},
    },
    {
        "port": 8788,
        "service": "Surface lease plane + governed-effect plane (BEAM)",
        "host": "governance host",
        "source": "`LEASE_PLANE_BASE_URL` default — `src/services/runtime_queries.py`",
        "verify": "src/services/runtime_queries.py",
        "health": {"path": "/health", "expect": [200], "scope": "governance"},
    },
    {
        "port": 8789,
        "service": "Agent orchestrator (BEAM)",
        "host": "governance host",
        "source": "`AGENT_ORCHESTRATOR_URL` default — `src/mcp_handlers/dialectic/orchestrator_dispatch.py`",
        "verify": "src/mcp_handlers/dialectic/orchestrator_dispatch.py",
        # Bearer-gated: 401 is a HEALTHY answer here. Only a connection failure
        # (curl reports 000) means down. Expecting 200 alone pages every cycle.
        "health": {"path": "/health", "expect": [200, 401], "scope": "governance"},
    },
    {
        "port": 8790,
        "service": "Dialectic-live (Phoenix/LiveView, BEAM)",
        "host": "governance host",
        "source": "`scripts/ops/com.unitares.dialectic-live.plist.template`",
        "verify": "scripts/ops/com.unitares.dialectic-live.plist.template",
        "health": {"path": "/health", "expect": [200], "scope": "governance"},
    },
    {
        "port": 8770,
        "service": "Wave 3A handlers (BEAM)",
        "host": "governance host",
        "source": "`elixir/wave3a_handlers/config/config.exs` — `WAVE_3A_HANDLERS_PORT` default",
        "verify": "elixir/wave3a_handlers/config/config.exs",
        "health": {"path": "/health", "expect": [200], "scope": "governance"},
    },
]


def health_probes(scope: str = "governance") -> list[dict]:
    """Probe descriptors for every registry surface reachable in ``scope``.

    The doc drift this generator was built to stop had a second victim nobody
    noticed: ``scripts/ops/health_watchdog.sh`` hand-enumerated two services
    while the registry declared six. Port 8768 crash-looped for eight days
    behind that gap, and the hourly deploy doctor kept reporting every surface
    healthy — truthfully, because 8768 was not one of its surfaces.

    So the watchdog reads its roster from here instead of keeping its own.
    Adding a service to ``PORTS`` now monitors it; forgetting to is caught by
    ``tests/test_ports_catalog_health_coverage.py`` rather than by an outage.

    ``expect`` is a list because reachable does not mean 200: the orchestrator
    on 8789 is bearer-gated and answers 401 to an unauthenticated probe. A
    genuinely dead port yields neither — curl reports 000.
    """
    out = []
    for p in sorted(PORTS, key=lambda p: p["port"]):
        h = p.get("health")
        if not h or h.get("scope") != scope:
            continue
        out.append({
            "name": p["service"],
            "port": p["port"],
            "url": f"http://127.0.0.1:{p['port']}{h['path']}",
            "expect": h["expect"],
        })
    return out


def render_health_probes(scope: str = "governance") -> str:
    """The probe roster as TSV: name, url, accepted codes (comma-joined).

    Consumed by ``scripts/ops/health_watchdog.sh``. Tab-separated because
    service names contain spaces and parentheses.
    """
    return "\n".join(
        f"{d['name']}\t{d['url']}\t{','.join(str(c) for c in d['expect'])}"
        for d in health_probes(scope)
    )


def verify_sources() -> list[str]:
    """Return a list of failures: registry ports whose cited source no longer
    contains the literal (i.e. the code moved but the registry did not)."""
    failures = []
    for p in PORTS:
        rel = p["verify"]
        if not rel:
            continue
        f = REPO / rel
        if not f.exists():
            failures.append(f"{p['port']}: source file missing: {rel}")
            continue
        if str(p["port"]) not in f.read_text(encoding="utf-8", errors="replace"):
            failures.append(
                f"{p['port']}: not found in cited source {rel} — "
                f"the port moved; update PORTS in scripts/dev/ports_catalog.py"
            )
    return failures


def render() -> str:
    rows = "\n".join(
        f"| `{p['port']}` | {p['service']} | {p['host']} | {p['source']} |"
        for p in sorted(PORTS, key=lambda p: p["port"])
    )
    return f"""<!-- GENERATED by scripts/dev/ports_catalog.py — do not edit by hand. Re-run to refresh. -->
# Definitive Ports

Status: generated registry — do not hand-edit; re-run the generator.

Canonical service-port registry. **Generated** by `scripts/dev/ports_catalog.py`
from the `PORTS` declaration — edit that list (and the cited source), then re-run.
Every in-repo port is cross-checked against its source file by the generator, so
this table cannot silently disagree with the code.

## Standard Assignments

| Port | Service | Host | Canonical source |
|------|---------|------|------------------|
{rows}

Default bind is `127.0.0.1` for the governance-host services; LAN/tunnel exposure
is opt-in per service (e.g. `UNITARES_BIND_ALL_INTERFACES`, `DIALECTIC_LIVE_BIND_ALL`).
The lease/effect plane (`8788`) is bearer-gated (`LEASE_PLANE_BEARER_TOKEN`).

## Verification

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep -E '87(6[67]|88|89|90)'
curl -s http://127.0.0.1:8767/health
```

Anima (`8766`) is an external service on the Pi — verify it against the running
anima-mcp deployment, not this repo.

## Read Next

- [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md): operator workflow and failure handling
- [../dev/CANONICAL_SOURCES.md](../dev/CANONICAL_SOURCES.md): authority ordering
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the doc is stale or a source lost its port")
    ap.add_argument("--health-probes", action="store_true",
                    help="print the health-probe roster as TSV (name, url, accepted codes)")
    args = ap.parse_args()

    if args.health_probes:
        # Deliberately independent of verify_sources(): the watchdog must still
        # get a roster when a cited source has drifted, or one registry typo
        # would silently disable monitoring for every service at once.
        print(render_health_probes())
        return 0

    content = render()
    src_failures = verify_sources()

    if args.check:
        rc = 0
        if src_failures:
            print("Port source drift:\n  " + "\n  ".join(src_failures), file=sys.stderr)
            rc = 1
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != content:
            print(f"{OUT.relative_to(REPO)} is stale — run: python3 scripts/dev/ports_catalog.py",
                  file=sys.stderr)
            rc = 1
        if rc == 0:
            print(f"{OUT.relative_to(REPO)} is up to date.")
        return rc

    if src_failures:
        print("Refusing to write — port source drift:\n  " + "\n  ".join(src_failures),
              file=sys.stderr)
        return 1
    OUT.write_text(content, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(REPO)} ({len(PORTS)} ports).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
