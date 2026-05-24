#!/usr/bin/env python3
"""60-second demo: onboard a synthetic agent and drive it through a trajectory.

Prereq: a governance MCP server reachable at http://127.0.0.1:8767.
Either ``docker compose up -d --wait`` or
``python src/mcp_server.py --port 8767``.

Run::

    make demo
    # or: python3 scripts/demo/quick_demo.py

If the server is on another host-side port, set UNITARES_DEMO_PORT=18767 or
UNITARES_DEMO_URL=http://127.0.0.1:18767/v1/tools/call.

What you'll see:
- The agent onboards (fresh UUID + thread).
- Seven check-ins simulate clean work → calibration drift → confusion.
- Each step prints the verdict, the reason, and the four-channel state.
- A trajectory summary shows the risk_score climb from ~0.27 to >1.0.

No Postgres reads, no dashboard required — everything you see comes back
in the check-in response shape that any client would receive.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _rest_url() -> str:
    """Return the REST tools endpoint used by the demo."""
    explicit = os.environ.get("UNITARES_DEMO_URL")
    if explicit:
        return explicit
    port = (
        os.environ.get("UNITARES_DEMO_PORT")
        or os.environ.get("GOVERNANCE_HOST_PORT")
        or "8767"
    )
    return f"http://127.0.0.1:{port}/v1/tools/call"


REST = _rest_url()


def _health_url() -> str:
    """Return the liveness endpoint matching REST's scheme/host/port."""
    parsed = urllib.parse.urlsplit(REST)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/health/live", "", ""))

# (response_text, complexity_self_report, confidence_self_report)
# The story: 3 clean check-ins, then a calibration miss (claims easy but the
# text describes hard work), then declining confidence, then an overconfident
# garbled output. Numbers are realistic for what an agent loop would emit.
TRAJECTORY = [
    ("Refactored auth middleware; tests pass.",                              0.3, 0.85),
    ("Added rate limiter; rolled out behind flag.",                          0.35, 0.85),
    ("Reviewed PR #482, requested one change.",                              0.25, 0.9),
    # Calibration drift: agent claims trivial, text says otherwise.
    ("Rewrote the session-pool acquisition path under contention; "
     "added 3 new invariants, reworked the lease ladder, "
     "and shimmed the asyncpg cursor wrap.",                                  0.25, 0.85),
    # Confidence starts slipping.
    ("Investigated a 5xx spike. Possibly the cache layer? "
     "Not sure if it's the same as the Tuesday incident.",                    0.6, 0.55),
    # Confused, scattered.
    ("Looked at logs, tried a few things, reverted some of it. "
     "Going to look again tomorrow.",                                         0.7, 0.4),
    # Overconfident on garbled output.
    ("DONE. All systems green. Migration complete. "
     "(Note: did not actually run migrations on staging.)",                   0.2, 0.95),
]


def call(tool: str, args: dict) -> dict:
    body = json.dumps({"name": tool, "arguments": args}).encode()
    req = urllib.request.Request(
        REST, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["result"]


def preflight() -> None:
    health = _health_url()
    try:
        urllib.request.urlopen(health, timeout=3)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        sys.exit(
            f"Governance server not reachable at {health}.\n"
            "Start it first:\n"
            "    docker compose up -d --wait        # bundled stack\n"
            "    # or:\n"
            "    python src/mcp_server.py --port 8767   # bare-metal\n"
            "If you changed the host port, run e.g.:\n"
            "    UNITARES_DEMO_PORT=18767 make demo\n"
            f"\nError: {e}"
        )


def banner(title: str) -> None:
    print(f"\n{'─' * 64}\n  {title}\n{'─' * 64}")


def fmt_metrics(m: dict) -> str:
    # risk_score: smoothed mean-of-10, the value make_decision gated on
    # risk_score_latest: raw last observation (spike), shown alongside
    latest = m.get("risk_score_latest")
    latest_str = f" latest={latest:.2f}" if latest is not None else ""
    return (
        f"E={m['E']:+.2f} I={m['I']:+.2f} S={m['S']:+.2f} V={m['V']:+.2f}  "
        f"coh={m['coherence']:.2f} risk={m['risk_score']:.2f}{latest_str}"
    )


def main() -> int:
    preflight()

    banner("1. onboard a synthetic agent")
    onboard = call(
        "onboard",
        {"name": "quick-demo-agent", "model_type": "resident_agent", "force_new": True},
    )
    session = onboard["client_session_id"]
    print(f"   agent_uuid       = {onboard['uuid']}")
    print(f"   client_session   = {session}")
    print(f"   thread           = {onboard.get('welcome', '').rsplit(' ', 1)[-1]}")

    banner("2. seven check-ins (clean → drift → confusion)")
    for i, (text, complexity, confidence) in enumerate(TRAJECTORY, 1):
        r = call(
            "process_agent_update",
            {
                "response_text": text,
                "complexity": complexity,
                "confidence": confidence,
                "client_session_id": session,
                "response_mode": "compact",
            },
        )
        d = r["decision"]
        m = r["metrics"]
        print(
            f"\n   step {i}: verdict = {d['action']:7s}"
            f"  ({d.get('margin','-')})"
        )
        print(f"     said: \"{text[:70]}{'…' if len(text) > 70 else ''}\"")
        print(f"     self-report: complexity={complexity} confidence={confidence}")
        print(f"     {fmt_metrics(m)}")
        print(f"     reason: {d['reason']}")

    banner("3. what just happened")
    print("  Two risk signals to watch in each step above:")
    print()
    print("  • risk         = smoothed mean of the last 10 observations.")
    print("                   This is the value `make_decision` gates on, and")
    print("                   the percentage you see in `decision.reason`.")
    print("  • latest       = raw last observation. A single bad check-in can")
    print("                   spike this without moving the gating signal.")
    print()
    print("  On this 7-step cold-agent trajectory, latest climbs sharply (raw")
    print("  signal sees the drift) while risk stays low (smoothing damps a")
    print("  short history). Verdicts therefore stay `proceed`.")
    print()
    print("  This is the system being conservative on a fresh agent —")
    print("  self-relative scoring needs ~30 check-ins to build a Welford")
    print("  baseline. On a warm agent the same drift would shift the smoothed")
    print("  risk faster and flip to `guide` or `pause`. The honest read of")
    print("  this demo: latest tells you what just happened, risk tells you")
    print("  what the gate believes — and the gap between them is the cold-")
    print("  start cost.")

    banner("done")
    print("  • Every number above came from check-in responses — no DB queries.")
    print("  • Open http://localhost:8767/dashboard to see the same state visually.")
    print("  • Integrate this in your own agent loop: 5 lines, see README §Quick Start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
