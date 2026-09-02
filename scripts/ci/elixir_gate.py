#!/usr/bin/env python3
"""The one Elixir context worth requiring on master (#2040).

Reads the ``needs`` context of the ``elixir-gate`` job (``NEEDS_JSON``, the
workflow passes ``toJSON(needs)``) and exits non-zero unless the run is
trustworthy:

* every suite job either succeeded or was skipped;
* a skipped suite is only acceptable when the detector job succeeded AND said
  nothing relevant changed. If the detector reported relevant, or the detector
  itself did not succeed (the workflow then runs every suite regardless), a
  suite that did not run is a defect, not a pass.

Anything else (``failure``, ``cancelled``, an unknown result) fails the gate.
The gate runs with ``if: always()`` so it always reports a status, which is
what makes it requirable where the path-filtered per-app jobs are not.

Break-glass note for the maintainer: branch protection on master has
``enforce_admins`` on, so once this context is required, a red gate on every
branch (the 2026-08-01 audit-partition shape) is cleared by fixing the cause
on a branch whose suites then pass -- or, if the suites themselves cannot be
made green, by temporarily removing the context from the required list. That
is the same escape every required context here already has.
"""

from __future__ import annotations

import json
import os
import sys

DETECTOR_JOB = "elixir_changes"


def evaluate(needs: dict) -> tuple[bool, list[str]]:
    """Return (ok, human-readable lines describing every job's disposition)."""
    lines: list[str] = []
    ok = True

    detector = needs.get(DETECTOR_JOB)
    if detector is None:
        ok = False
        lines.append(f"FAIL {DETECTOR_JOB}: missing from needs; the gate is miswired")
        detector = {}
    detector_result = detector.get("result")
    reported_relevant = str((detector.get("outputs") or {}).get("relevant", "")).lower() == "true"
    if detector_result == "success":
        must_run = reported_relevant
        lines.append(f"ok   {DETECTOR_JOB}: relevant={'true' if reported_relevant else 'false'}")
    else:
        # A detector that did not succeed cannot vouch for a skip. The workflow
        # runs every suite in that case, so the gate requires all of them.
        must_run = True
        lines.append(f"note {DETECTOR_JOB}: result={detector_result!r}; every suite must have run and passed")

    suites = sorted(name for name in needs if name != DETECTOR_JOB)
    if not suites:
        ok = False
        lines.append("FAIL no suite jobs in needs; the gate is miswired")

    for name in suites:
        result = (needs.get(name) or {}).get("result")
        if result == "success":
            lines.append(f"ok   {name}: success")
        elif result == "skipped" and not must_run:
            lines.append(f"ok   {name}: skipped (nothing relevant changed)")
        elif result == "skipped":
            ok = False
            lines.append(f"FAIL {name}: skipped although the suites had to run")
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
