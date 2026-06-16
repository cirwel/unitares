"""Single-episode live verification of the calibration spine.

Build-order gate (handoff): confirm the end-to-end path lands a *corroborated*
outcome (evidence_weight == 1.0) and populates the tactical channel BEFORE
scaling to 200. Runs two episodes under one dedicated, quarantined harness
identity:

  * a clean_control pass  (high confidence, exit 0  -> test_passed)
  * an overconfidence probe (high confidence, exit 1 -> test_failed, bad_rate>0)

Run:  python -m scripts.dev.calibration_harness.probe_one
      (from the repo root; needs UNITARES_HTTP_API_TOKEN + a live server)
"""
from __future__ import annotations

import argparse
from collections.abc import Sequence

from .client import GovernanceClient
from .config import MIN_TACTICAL_EVIDENCE_WEIGHT, Transport
from .grader import grade_script
from .run_v1 import _guard_not_prod

PASS_SCRIPT = "assert 1 + 1 == 2\n"
FAIL_SCRIPT = "import sys\nassert 2 + 2 == 5, 'seeded failure'\nsys.exit(0)\n"


def _dig(d: dict, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _find_evidence_weight(resp: dict):
    # be tolerant of nesting until we pin the exact shape from live output
    for path in (("evidence_weight",), ("detail", "evidence_weight"), ("result", "evidence_weight")):
        v = _dig(resp, *path)
        if v is not None:
            return v, path
    return None, None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the one-shot calibration probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-know", action="store_true", help="bypass the prod-URL guard")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    transport = Transport()
    _guard_not_prod(transport.base_url, args.i_know)
    client = GovernanceClient(transport)

    print("== onboard (dedicated quarantined identity) ==")
    ident = client.onboard("calib-harness-probe")
    print(f"agent_uuid={ident.agent_uuid}  client_session_id={ident.client_session_id}")
    print("onboard result keys:", list(ident.raw)[:20])

    def _tests_count() -> int:
        te = (client.calibration_check(ident).get("tactical_evidence", {}) or {})
        return (te.get("signal_sources") or {}).get("tests") or 0

    before_tests = _tests_count()

    episodes = [
        ("clean_control", 0.90, PASS_SCRIPT, False),
        ("overconfidence_probe", 0.90, FAIL_SCRIPT, True),
    ]
    ok = True
    for label, conf, src, expect_bad in episodes:
        print(f"\n== episode: {label} (confidence={conf}) ==")
        pred_id = client.check_in(
            ident,
            confidence=conf,
            response_text=f"[{label}] attempting bounded task; expect {'fail' if expect_bad else 'pass'}",
            task_label=label,
        )
        print(f"prediction_id={pred_id}")

        grade = grade_script(src, label=label)
        print(f"grade: is_bad={grade.is_bad} exit_code={grade.exit_code} score={grade.score}")
        assert grade.is_bad == expect_bad, f"grader disagreed with construction for {label}"

        out = client.record_outcome(
            ident,
            prediction_id=pred_id,
            is_bad=grade.is_bad,
            outcome_score=grade.score,
            detail=grade.detail,
        )
        ew, path = _find_evidence_weight(out)
        print(f"outcome response keys: {list(out)[:20]}")
        print(f"evidence_weight={ew}  (found at {path})")
        if ew is None or float(ew) < MIN_TACTICAL_EVIDENCE_WEIGHT:
            print(f"  !! GATE FAIL: need evidence_weight >= {MIN_TACTICAL_EVIDENCE_WEIGHT} to register tactically")
            ok = False

    print("\n== quarantine check (synthetic rows must NOT train calibration) ==")
    after_tests = _tests_count()
    delta = after_tests - before_tests
    print(f"tactical 'tests' count: {before_tests} -> {after_tests} (delta={delta})")
    # The grader marks every row synthetic_calibration_fixture=True, so the
    # server-side guard excludes them: the global channel must NOT gain our rows.
    if delta >= len(episodes):
        print("  !! GATE FAIL: synthetic rows registered into the global channel (exclusion broken)")
        ok = False
    else:
        print(f"  synthetic rows excluded from the global channel (delta < {len(episodes)}) — quarantine OK")

    print("\nRESULT:", "PASS — binding corroborated + quarantine enforced" if ok else "FAIL — fix before scaling")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
