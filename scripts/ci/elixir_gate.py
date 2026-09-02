#!/usr/bin/env python3
"""The one Elixir context worth requiring on master (#2040).

Reads the ``needs`` context of the ``elixir-gate`` job (``NEEDS_JSON``, the
workflow passes ``toJSON(needs)``) and exits non-zero unless the run is
trustworthy:

* the ``changes`` detector itself succeeded (a failed detector must not pass
  the gate by default);
* every suite job either succeeded or was skipped;
* a skipped suite is only acceptable when the detector said nothing relevant
  changed. If ``relevant=true`` and a suite did not run, that is a workflow
  defect, not a pass.

Anything else (``failure``, ``cancelled``, an unknown result) fails the gate.
The gate runs with ``if: always()`` so it always reports a status, which is
what makes it requirable where the path-filtered per-app jobs are not.
"""

from __future__ import annotations

import json
import os
import sys

DETECTOR_JOB = "changes"
PASSING_RESULTS = frozenset({"success", "skipped"})


def evaluate(needs: dict) -> tuple[bool, list[str]]:
    """Return (ok, human-readable lines describing every job's disposition)."""
    lines: list[str] = []
    ok = True

    detector = needs.get(DETECTOR_JOB) or {}
    detector_result = detector.get("result")
    relevant = str((detector.get("outputs") or {}).get("relevant", "")).lower() == "true"
    if detector_result != "success":
        ok = False
        lines.append(f"FAIL {DETECTOR_JOB}: result={detector_result!r}; the gate cannot trust a run whose detector did not succeed")
    else:
        lines.append(f"ok   {DETECTOR_JOB}: relevant={'true' if relevant else 'false'}")

    suites = sorted(name for name in needs if name != DETECTOR_JOB)
    if not suites:
        ok = False
        lines.append("FAIL no suite jobs in needs; the gate is miswired")

    for name in suites:
        result = (needs.get(name) or {}).get("result")
        if result == "success":
            lines.append(f"ok   {name}: success")
        elif result == "skipped" and not relevant:
            lines.append(f"ok   {name}: skipped (nothing relevant changed)")
        elif result == "skipped":
            ok = False
            lines.append(f"FAIL {name}: skipped although relevant paths changed")
        else:
            ok = False
            lines.append(f"FAIL {name}: result={result!r}")

    return ok, lines


def main() -> int:
    raw = os.environ.get("NEEDS_JSON", "")
    try:
        needs = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        print(f"::error::NEEDS_JSON is not valid JSON: {exc}")
        return 1
    if not isinstance(needs, dict):
        print("::error::NEEDS_JSON must be the toJSON(needs) object")
        return 1

    ok, lines = evaluate(needs)
    for line in lines:
        print(line)
    if ok:
        print("elixir-gate: PASS")
        return 0
    print("::error::elixir-gate: FAIL (see job dispositions above)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
