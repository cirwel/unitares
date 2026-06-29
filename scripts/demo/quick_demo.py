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
- The graded proprioceptive signal moves in the drift direction: entropy S
  rises, integrity I slips, valence V swings, and the decision margin tightens
  (settling → tight) as the calibration miss and confusion accumulate.
- The verdict stays `proceed` across this short run. Governance gates on a
  smoothed risk (mean-of-10), which a seven-step synthetic trajectory does not
  push over the pause threshold — by design, to avoid false pauses on normal
  work. A `proceed` whose margin has gone `tight` is the signal here, not a
  pause. Sustained or higher-severity drift can cross the gate and pause; the
  code still handles that AGENT_PAUSED reply, you just won't trip it in 7 steps.

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
    latest_str = f" latest={fmt_float(latest)}" if latest is not None else ""
    return (
        f"E={fmt_float(m.get('E'), signed=True)} "
        f"I={fmt_float(m.get('I'), signed=True)} "
        f"S={fmt_float(m.get('S'), signed=True)} "
        f"V={fmt_float(m.get('V'), signed=True)}  "
        f"coh={fmt_float(m.get('coherence'))} "
        f"risk={fmt_float(m.get('risk_score'))}{latest_str}"
    )


def fmt_float(value: object, *, signed: bool = False) -> str:
    if isinstance(value, bool) or value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:+.2f}" if signed else f"{number:.2f}"


def _nonempty_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_text(*values: object) -> str | None:
    for value in values:
        text = _nonempty_text(value)
        if text is not None:
            return text
    return None


def extract_decision(result: dict) -> dict:
    """Return a decision-like dict from compact, mirror, minimal, or standard shapes."""
    if not isinstance(result, dict):
        raise TypeError("process_agent_update result must be a JSON object")

    decision = result.get("decision")
    if isinstance(decision, dict):
        action = _first_text(decision.get("action"), result.get("action"))
        if action:
            return {
                "action": action,
                "reason": _first_text(decision.get("reason"), result.get("reason"), result.get("summary"))
                or "No reason supplied.",
                "margin": _first_text(decision.get("margin"), result.get("margin")) or "-",
            }
    elif isinstance(decision, str):
        return {
            "action": decision,
            "reason": _first_text(result.get("reason"), result.get("summary")) or "No reason supplied.",
            "margin": _first_text(result.get("margin")) or "-",
        }

    verdict = result.get("verdict")
    extracted = _extract_verdict(verdict, result)
    if extracted:
        return extracted

    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        extracted = _extract_verdict(metrics.get("verdict"), result)
        if extracted:
            return extracted

    action = _first_text(result.get("action"))
    if action:
        return {
            "action": action,
            "reason": _first_text(result.get("reason"), result.get("summary")) or "No reason supplied.",
            "margin": _first_text(result.get("margin")) or "-",
        }

    keys = ", ".join(sorted(str(key) for key in result.keys()))
    raise KeyError(f"process_agent_update result missing decision/verdict/action; keys: {keys}")


def _extract_verdict(raw_verdict: object, result: dict) -> dict | None:
    if isinstance(raw_verdict, dict):
        action = _first_text(
            raw_verdict.get("value"),
            raw_verdict.get("action"),
            raw_verdict.get("verdict"),
        )
        if not action:
            return None
        return {
            "action": action,
            "reason": _first_text(
                result.get("reason"),
                raw_verdict.get("reason"),
                raw_verdict.get("meaning"),
                raw_verdict.get("next_action"),
                result.get("summary"),
            )
            or "No reason supplied.",
            "margin": _first_text(result.get("margin"), raw_verdict.get("margin")) or "-",
        }
    if isinstance(raw_verdict, str):
        return {
            "action": raw_verdict,
            "reason": _first_text(result.get("reason"), result.get("summary")) or "No reason supplied.",
            "margin": _first_text(result.get("margin")) or "-",
        }
    return None


def extract_metrics(result: dict) -> dict:
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    return {
        "E": result.get("E"),
        "I": result.get("I"),
        "S": result.get("S"),
        "V": result.get("V"),
        "coherence": result.get("coherence"),
        "risk_score": result.get("risk_score"),
        "risk_score_latest": result.get("risk_score_latest"),
    }


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
        try:
            d = extract_decision(r)
        except KeyError:
            # A check-in on an agent that has crossed the high-risk gate returns
            # an AGENT_PAUSED circuit-breaker reply with no decision-like field.
            note = r.get("error") or r.get("message") or "agent paused"
            print(f"\n   step {i}: check-in refused — {note}")
            break
        m = extract_metrics(r)
        print(
            f"\n   step {i}: verdict = {d['action']:7s}"
            f"  ({d.get('margin','-')})"
        )
        print(f"     said: \"{text[:70]}{'…' if len(text) > 70 else ''}\"")
        print(f"     self-report: complexity={complexity} confidence={confidence}")
        print(f"     {fmt_metrics(m)}")
        print(f"     reason: {d.get('reason', 'No reason supplied.')}")

    banner("3. what just happened")
    print("  Two risk signals to watch in each step above:")
    print()
    print("  • risk         = smoothed mean of the last 10 observations.")
    print("                   This is the value `make_decision` gates on, and")
    print("                   the percentage you see in `decision.reason`.")
    print("  • latest       = raw last observation. A single bad check-in can")
    print("                   spike this without moving the gating signal.")

    banner("done")
    print("  • Every number above came from check-in responses — no DB queries.")
    print("  • Open http://localhost:8767/dashboard to see the same state visually.")
    print("  • Integrate this in your own agent loop: 5 lines, see README §Quick Start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
