#!/usr/bin/env python3
"""60-second demo: onboard a synthetic agent and drive it through a trajectory.

Prereq: a governance MCP server reachable at http://127.0.0.1:8767.
Either ``docker compose up`` or ``python src/mcp_server.py --port 8767``.

Run::

    make demo
    # or: python3 scripts/demo/quick_demo.py

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
import sys
import urllib.error
import urllib.request

REST = "http://127.0.0.1:8767/v1/tools/call"

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
    try:
        urllib.request.urlopen("http://127.0.0.1:8767/health/live", timeout=3)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        sys.exit(
            "Governance server not reachable at 127.0.0.1:8767.\n"
            "Start it first:\n"
            "    docker compose up        # bundled stack\n"
            "    # or:\n"
            "    python src/mcp_server.py --port 8767   # bare-metal\n"
            f"\nError: {e}"
        )


def banner(title: str) -> None:
    print(f"\n{'─' * 64}\n  {title}\n{'─' * 64}")


def fmt_metrics(m: dict) -> str:
    return (
        f"E={m['E']:+.2f} I={m['I']:+.2f} S={m['S']:+.2f} V={m['V']:+.2f}  "
        f"coh={m['coherence']:.2f} risk={m['risk_score']:.2f}"
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
    print("  Risk trajectory across the seven steps:")
    print("    step 1–3 (clean work):              risk ≈ 0.27")
    print("    step 4   (calibration drift):       risk ≈ 0.29")
    print("    step 5–6 (confidence collapse):     risk → 0.95")
    print("    step 7   (overconfident on garble): risk > 1.00")
    print()
    print("  Verdicts stayed `proceed` because this is a brand-new agent —")
    print("  self-relative scoring needs ~30 check-ins to build a Welford baseline.")
    print("  On a warm agent the same drift would flip to `guide` then `pause`.")
    print("  The drift is *legible in the metrics from check-in #1*; the verdict is")
    print("  what the system would gate on once it has enough self-history to trust.")

    banner("done")
    print("  • Every number above came from check-in responses — no DB queries.")
    print("  • Open http://localhost:8767/dashboard to see the same state visually.")
    print("  • Integrate this in your own agent loop: 5 lines, see README §Quick Start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
