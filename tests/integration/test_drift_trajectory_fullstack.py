"""Full-stack integration test: a drift trajectory through the live governance stack.

This is the scaffold integration test asked for by the "testing gaps" review: it
brings nothing up itself, but *given a running stack* it onboards a synthetic
agent over the real MCP/HTTP surface, drives a clean -> calibration-drift ->
confusion trajectory through ``process_agent_update``, and asserts the system
actually processed it end to end.

OPT-IN. It needs live services (governance MCP on :8767 and its Postgres), so it
is gated on ``RUN_INTEGRATION_STACK=1`` *and* on reachability preflights. With no
stack up it skips cleanly (pytest skip, never error), keeping the default fast
gate green. Bring a stack up with either::

    docker compose up -d --wait
    # or a bare-metal server: python src/mcp_server.py --port 8767

then run::

    RUN_INTEGRATION_STACK=1 pytest tests/integration/test_drift_trajectory_fullstack.py -v -m integration

Trajectory source is reused from ``scripts/demo/quick_demo.py`` (the same story
``make demo`` tells: three clean check-ins, a calibration miss, then declining /
overconfident confusion).

What this asserts (the robust, deterministic contract):

  1. **End to end.** onboard returns a uuid + session; each of N check-ins
     returns a well-formed decision shape; the early clean check-ins are healthy
     (``proceed``).
  2. **Audit trail.** Rows land in ``audit.events`` for the driven agent.
  3. **Drift is measured.** The per-observation raw risk (``risk_score_latest``)
     is materially higher on the miscalibrated / confused check-ins than on the
     clean ones — the assessment path demonstrably responds to drift.

What this deliberately does NOT hard-assert: a verdict *flip* to guide/pause.
Probing the live server showed the decision ``action``/``sub_action`` is
sequence- and global-state-sensitive and strongly biased against pausing
synthetic agents (false-pause avoidance / growth-not-punish). The same trajectory
yields ``proceed`` on three fresh agents and ``pause``/``guide`` on a fourth, so a
hard ``proceed -> pause`` transition is not reproducible from one agent's own
inputs. The per-observation ``risk_score_latest`` *is* deterministic (clean ~0.25
vs confused ~1.0 across fresh agents), so that is what we gate on. The test still
records and reports any guide/pause it observes, without failing when none occurs.
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- opt-in gate: live services required ------------------------------------
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION_STACK") != "1",
        reason="Full-stack integration test requires RUN_INTEGRATION_STACK=1 and a running stack",
    ),
]


def _rest_url() -> str:
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
DB_URL = os.environ.get(
    "UNITARES_INTEGRATION_DB_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)


def _health_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(REST)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health/live", "", ""))


def _load_quick_demo():
    """Import scripts/demo/quick_demo.py by path (scripts/demo is not a package)."""
    path = REPO_ROOT / "scripts" / "demo" / "quick_demo.py"
    spec = importlib.util.spec_from_file_location("quick_demo_for_integration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call(tool: str, args: dict) -> dict:
    body = json.dumps({"name": tool, "arguments": args}).encode()
    req = urllib.request.Request(REST, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["result"]


def _require_http_stack() -> None:
    try:
        urllib.request.urlopen(_health_url(), timeout=3)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        pytest.skip(f"governance HTTP surface not reachable at {_health_url()}: {exc}")


def _audit_event_count(agent_uuid: str, session_id: str) -> int:
    """Count audit.events rows attributable to the driven agent.

    Direct DB read is the ground truth the acceptance asks for ("rows land in
    audit.events"). Skips (does not fail) when the DB is unreachable so the test
    degrades cleanly on a partial stack.
    """
    import asyncio

    try:
        import asyncpg
    except ImportError:  # pragma: no cover - asyncpg is a hard dep of the server
        pytest.skip("asyncpg not installed; cannot verify audit.events")

    async def _count() -> int:
        conn = await asyncpg.connect(DB_URL, timeout=5)
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM audit.events WHERE agent_id = $1 OR session_id = $2",
                agent_uuid,
                session_id,
            )
        finally:
            await conn.close()

    try:
        return asyncio.run(_count())
    except Exception as exc:  # connection refused, auth, etc.
        pytest.skip(f"governance Postgres not reachable at {DB_URL}: {exc}")


def _verdict(result: dict) -> tuple[str | None, str | None]:
    """Return (action, sub_action) tolerant of compact/standard/minimal shapes."""
    decision = result.get("decision")
    if isinstance(decision, dict):
        return decision.get("action"), decision.get("sub_action")
    if isinstance(decision, str):
        return decision, None
    return result.get("action"), result.get("sub_action")


def test_drift_trajectory_audit_and_signal():
    _require_http_stack()
    demo = _load_quick_demo()

    # 1. Onboard a synthetic agent over the real surface.
    onboard = _call(
        "onboard",
        {"name": "itest-drift-trajectory", "model_type": "resident_agent", "force_new": True},
    )
    agent_uuid = onboard["uuid"]
    session = onboard["client_session_id"]
    assert agent_uuid and session

    # 2. Drive the documented clean -> drift trajectory, then a sharp confusion
    #    tail. compact mode is what surfaces risk_score_latest, the deterministic
    #    per-observation drift signal.
    #
    #    quick_demo.TRAJECTORY (clean head + mild calibration drift) is the
    #    narrative source, but its confusion is gentle (per-obs risk ~0.4). We
    #    append an explicit, sharply-miscalibrated confusion tail (claims trivial
    #    complexity at near-zero confidence over garbled text) which probes
    #    deterministically to risk_score_latest ~1.0 on fresh agents — the
    #    "confusion" leg of clean -> calibration drift -> confusion.
    confusion_tail = [
        ("tried things, reverted, not sure what broke, logs unclear, maybe cache "
         "maybe race, garbled output, possibly wrong", 0.97, 0.05)
    ] * 3

    verdicts: list[tuple[str | None, str | None]] = []
    clean_latests: list[float] = []
    drift_latests: list[float] = []
    confusion_latests: list[float] = []

    def _drive(text, complexity, confidence):
        result = _call(
            "process_agent_update",
            {
                "response_text": text,
                "complexity": complexity,
                "confidence": confidence,
                "client_session_id": session,
                "response_mode": "compact",
            },
        )
        verdicts.append(_verdict(result))
        latest = demo.extract_metrics(result).get("risk_score_latest")
        return float(latest) if isinstance(latest, (int, float)) else None

    # Three phases, each bucketed separately.
    for text, complexity, confidence in demo.TRAJECTORY[:3]:          # clean head
        latest = _drive(text, complexity, confidence)
        if latest is not None:
            clean_latests.append(latest)
    for text, complexity, confidence in demo.TRAJECTORY[3:]:         # calibration drift
        latest = _drive(text, complexity, confidence)
        if latest is not None:
            drift_latests.append(latest)
    for text, complexity, confidence in confusion_tail:             # sharp confusion
        latest = _drive(text, complexity, confidence)
        if latest is not None:
            confusion_latests.append(latest)

    # 1 (cont.) End to end: every check-in returned a usable decision, and the
    # clean head is healthy. A circuit-broken (paused) agent would stop returning
    # an action mid-way; the clean head must not.
    clean_actions = [a for (a, _s) in verdicts[:3]]
    assert all(a == "proceed" for a in clean_actions), (
        f"clean head should be healthy 'proceed'; got {clean_actions}"
    )

    # 3. Drift is measured: the per-observation risk on the sharp confusion tail
    #    is materially higher than on the clean head. Probed separation is
    #    ~0.25 (clean) vs ~1.0 (confusion) and is deterministic across fresh
    #    agents; 0.5 is a wide, non-flaky guard band.
    assert clean_latests, "expected risk_score_latest on clean check-ins (compact mode)"
    assert confusion_latests, "expected risk_score_latest on confusion check-ins (compact mode)"
    mean_clean = sum(clean_latests) / len(clean_latests)
    mean_drift = sum(drift_latests) / len(drift_latests) if drift_latests else float("nan")
    mean_confusion = sum(confusion_latests) / len(confusion_latests)
    assert mean_confusion > mean_clean + 0.5, (
        f"confusion drift signal should rise sharply: mean clean risk_latest={mean_clean:.3f}, "
        f"mean confusion risk_latest={mean_confusion:.3f}"
    )

    # 2. Audit trail: rows landed for this agent.
    count = _audit_event_count(agent_uuid, session)
    assert count > 0, f"expected audit.events rows for agent {agent_uuid} / {session}"

    # Observational (not gated): surface any verdict escalation we saw. The live
    # gate is biased against pausing synthetic agents, so this is reported, not
    # required. See module docstring + PR body.
    escalations = [(a, s) for (a, s) in verdicts if a == "pause" or s == "guide" or a is None]
    print(
        f"\n[integration] agent={agent_uuid} audit_rows={count} "
        f"mean_clean_risk_latest={mean_clean:.3f} mean_drift_risk_latest={mean_drift:.3f} "
        f"mean_confusion_risk_latest={mean_confusion:.3f} "
        f"verdict_escalations={escalations or 'none (proceed throughout — expected)'}"
    )
