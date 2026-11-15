#!/usr/bin/env python3
"""Measure which tests depend on the EISV estimator, and on what about it.

Wraps ``UNITARESMonitor.process_update`` for one pytest session and compares
the failures each substitution produces:

``count``
    The real estimator. Records which tests reach it; changes nothing.
``freeze_proceed`` / ``freeze_pause``
    Every call returns one frozen estimator result and the monitor's state
    never advances. ``proceed`` is a real moderate result from a fresh
    monitor; ``pause`` is that same result carrying the verdict of a real
    first-check-in pause, with every verdict-derived field (``status``,
    ``policy_evaluation``, ``enforcement``, the behavioral verdict directive)
    rebuilt to match, so the two templates differ in the verdict and nothing
    else.
``out_proceed`` / ``out_pause``
    The real method runs, so state, side effects and gates inside it advance,
    and every output key is then replaced by the frozen result.

A test that fails under ``freeze_*`` but passes under ``out_*`` depends on the
estimator having advanced its state, not on its values. A test whose outcome
differs between ``*_proceed`` and ``*_pause`` depends on the verdict itself.

These are test-contract findings, not runtime dependency: a test that never
reaches the estimator says nothing, so ``count`` reports reach first. Note that
``out_*`` also overwrites decisions made by gates that live inside
``process_update`` (the verification floor today).

Usage:
    python3 scripts/diagnostics/eisv_substitution_probe.py
    python3 scripts/diagnostics/eisv_substitution_probe.py --modes freeze_pause out_pause
    python3 scripts/diagnostics/eisv_substitution_probe.py --files tests/test_governance_monitor.py
    python3 scripts/diagnostics/eisv_substitution_probe.py --json

Without ``--files`` the first run is ``count`` over ``tests/``; later modes run
only on the files whose tests reached the estimator. The probe's own tests are
never measured. A session that pytest did not complete (internal error,
interruption, a collection error in any target, nothing collected) or in which
the plugin did not report is an
incomplete run: the runner says so and exits 2 rather than reporting zero
failures. The pytest plugin is this module itself, loaded with
``-p eisv_substitution_probe`` by the runner.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
MODES = ("count", "freeze_proceed", "freeze_pause", "out_proceed", "out_pause")
DEFAULT_MODES = MODES

_PROCEED_INPUT = ({"response_text": "probe", "complexity": 0.5}, 0.7, "mixed")
_PAUSE_INPUT = (
    {"response_text": "probe", "complexity": 1.0, "ethical_drift": [0.9, 0.9, 0.9]},
    0.05,
    "convergent",
)


# Decision fields that carry the verdict. Measurement-derived decision fields
# (basin, margin, nearest_edge, ...) stay those of the proceed result.
VERDICT_DECISION_FIELDS = (
    "action", "sub_action", "reason", "guidance", "critical",
    "hard_stop_provenance", "cold_start_confirmation", "cold_start_epistemic_gate",
    "original_action", "original_sub_action", "gap_suppressed",
    "warmup_structural_suppressed", "cold_start_epistemic_deferred", "latest_risk_fast_trip",
)
# Top-level output keys whose value is the verdict or is derived from it. The
# pause template differs from the proceed template here only (plus the nested
# behavioral verdict directive), so a test that is sensitive to some other
# field (confidence, metrics, drift) is not reported as verdict-sensitive.
VERDICT_KEYS = ("decision", "status", "policy_evaluation", "enforcement")

_REAL_ATTR = "__eisv_probe_real__"


def original_process_update() -> Callable[..., Dict[str, Any]]:
    """The real ``process_update``, even while a probe substitute is installed."""
    from src.governance_monitor import UNITARESMonitor

    current = UNITARESMonitor.process_update
    return getattr(current, _REAL_ATTR, current)


def build_templates(real: Callable[..., Dict[str, Any]], monitor_cls: type) -> Dict[str, Dict[str, Any]]:
    """Build the ``proceed`` and ``pause`` templates from real estimator results.

    ``proceed`` is one real result. ``pause`` is that same result carrying the
    verdict of a real first-check-in pause: its decision takes the pause's
    ``VERDICT_DECISION_FIELDS`` over the proceed decision, ``status`` is the
    pause's, and every field derived from the decision (``policy_evaluation``,
    ``enforcement``, the behavioral verdict directive) is rebuilt with the
    repository's own builders from that decision and the proceed measurements.
    """
    real = getattr(real, _REAL_ATTR, real)
    results = {}
    for kind, (state, confidence, task_type) in (("proceed", _PROCEED_INPUT), ("pause", _PAUSE_INPUT)):
        result = real(monitor_cls(f"eisv-probe-{kind}"), dict(state), confidence=confidence, task_type=task_type)
        action = (result.get("decision") or {}).get("action")
        if action != kind:
            raise RuntimeError(f"template {kind!r} produced action {action!r}, expected {kind!r}")
        results[kind] = result
    return {"proceed": results["proceed"], "pause": with_verdict(results["proceed"], results["pause"])}


def with_verdict(base: Dict[str, Any], verdict_source: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``base`` carrying ``verdict_source``'s verdict and everything derived from it."""
    from src.agent_loop_detection import _rewrap_behavioral_verdict
    from src.monitor_result import _build_enforcement_stub, _build_policy_evaluation

    result = copy.deepcopy(base)
    decision = result["decision"]
    source_decision = verdict_source["decision"]
    for field in VERDICT_DECISION_FIELDS:
        if field in source_decision:
            decision[field] = copy.deepcopy(source_decision[field])
        else:
            decision.pop(field, None)
    result["status"] = verdict_source["status"]
    inputs = (base.get("policy_evaluation") or {}).get("inputs") or {}
    result["policy_evaluation"] = _build_policy_evaluation(
        decision, result["metrics"],
        latest_risk=inputs.get("risk_score_latest"),
        s_setpoint=inputs.get("s_setpoint"),
    )
    result["enforcement"] = _build_enforcement_stub(decision)
    _rewrap_behavioral_verdict(result, decision)
    return result


def make_substitute(
    mode: str,
    real: Callable[..., Dict[str, Any]],
    templates: Dict[str, Dict[str, Any]],
    on_call: Optional[Callable[[], None]] = None,
) -> Callable[..., Dict[str, Any]]:
    """Return a ``process_update`` replacement for ``mode``."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")

    def substitute(self, agent_state, confidence=None, task_type="mixed"):
        if on_call is not None:
            on_call()
        if mode == "count":
            return real(self, agent_state, confidence=confidence, task_type=task_type)
        template = templates[mode.split("_", 1)[1]]
        if mode.startswith("freeze"):
            return copy.deepcopy(template)
        result = real(self, agent_state, confidence=confidence, task_type=task_type)
        result.clear()
        result.update(copy.deepcopy(template))
        return result

    setattr(substitute, _REAL_ATTR, getattr(real, _REAL_ATTR, real))
    return substitute


def install(mode: str, on_call: Optional[Callable[[], None]] = None) -> Callable[[], None]:
    """Patch ``UNITARESMonitor.process_update`` for ``mode``; return an undo function."""
    from src.governance_monitor import UNITARESMonitor

    previous = UNITARESMonitor.process_update
    real = original_process_update()
    templates = {} if mode == "count" else build_templates(real, UNITARESMonitor)
    UNITARESMonitor.process_update = make_substitute(mode, real, templates, on_call)

    def undo() -> None:
        UNITARESMonitor.process_update = previous

    return undo


# --------------------------------------------------------------------------
# pytest plugin hooks (active only when EISV_PROBE_MODE is set)
# --------------------------------------------------------------------------

_current_test: List[Optional[str]] = [None]
_reached: set = set()


def _record_reach() -> None:
    if _current_test[0]:
        _reached.add(_current_test[0])


def pytest_sessionstart(session):  # pragma: no cover - exercised by the runner
    mode = os.environ.get("EISV_PROBE_MODE")
    if mode:
        install(mode, on_call=_record_reach)


def pytest_runtest_protocol(item, nextitem):  # pragma: no cover - exercised by the runner
    _current_test[0] = item.nodeid
    return None


def pytest_sessionfinish(session, exitstatus):  # pragma: no cover - exercised by the runner
    path = os.environ.get("EISV_PROBE_REACH")
    if os.environ.get("EISV_PROBE_MODE") and path:
        Path(path).write_text("\n".join(sorted(_reached)), encoding="utf-8")


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)")
# A short-summary ERROR naming a file (no "::") is a collection error, and
# pytest's own line says so; either means some targets were never measured.
_COLLECTION_ERROR_RE = re.compile(r"^ERROR [^\s:]+(?:\s|$)|errors? during collection", re.MULTILINE)

# pytest exit codes that describe a completed measurement: all passed, or
# some tests failed. Anything else (interrupted, internal error such as a
# template that cannot be built, usage error, nothing collected) is not one.
_MEASURED_EXIT_CODES = (0, 1)

# The probe's own tests install substitutes themselves; measuring them would
# report the probe, not the estimator.
SELF_TESTS = ("tests/test_eisv_substitution_probe.py",)


class IncompleteRun(RuntimeError):
    """A pytest session that did not produce a measurement."""


def run_mode(mode: str, targets: List[str], workdir: Path) -> Dict[str, Any]:
    if not targets:
        raise IncompleteRun(f"mode {mode!r}: no targets; pytest would fall back to default discovery")
    reach_path = workdir / f"reach_{mode}.txt"
    env = dict(os.environ)
    env["EISV_PROBE_MODE"] = mode
    env["EISV_PROBE_REACH"] = str(reach_path)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(Path(__file__).resolve().parent), env.get("PYTHONPATH")])
    )
    cmd = [
        sys.executable, "-m", "pytest", *targets, "-q", "-rfE",
        "-o", "addopts=", "-p", "no:cacheprovider", "-p", "eisv_substitution_probe",
        *(f"--ignore={path}" for path in SELF_TESTS),
    ]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    return parse_run(mode, proc.returncode, proc.stdout, proc.stderr, reach_path)


def parse_run(mode: str, returncode: int, stdout: str, stderr: str, reach_path: Path) -> Dict[str, Any]:
    """Turn one pytest session into a measurement, or raise ``IncompleteRun``."""
    tail = "\n".join((stdout + "\n" + stderr).strip().splitlines()[-15:])
    if returncode not in _MEASURED_EXIT_CODES:
        raise IncompleteRun(f"mode {mode!r}: pytest exited {returncode}, no measurement\n{tail}")
    if _COLLECTION_ERROR_RE.search(stdout):
        raise IncompleteRun(f"mode {mode!r}: collection errors, some targets were not measured\n{tail}")
    if not reach_path.exists():
        raise IncompleteRun(f"mode {mode!r}: the probe plugin did not report, no measurement\n{tail}")
    failed = sorted({m.group(1) for line in stdout.splitlines() if (m := _FAILED_RE.match(line))})
    summary = next((line for line in reversed(stdout.splitlines()) if line.strip()), "")
    reached = reach_path.read_text(encoding="utf-8").split()
    return {"mode": mode, "failed": failed, "reached": reached, "summary": summary}


def compare(results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    base = set(results.get("count", {}).get("failed", []))
    new = {m: sorted(set(r["failed"]) - base) for m, r in results.items() if m != "count"}
    report: Dict[str, Any] = {"new_failures": {m: len(v) for m, v in new.items()}}
    if "freeze_proceed" in new and "out_proceed" in new:
        report["state_advance_only"] = sorted(set(new["freeze_proceed"]) - set(new["out_proceed"]))
    for family in ("freeze", "out"):
        p, q = new.get(f"{family}_proceed"), new.get(f"{family}_pause")
        if p is not None and q is not None:
            report[f"{family}_verdict_sensitive"] = sorted(set(p) ^ set(q))
    report["new_failure_ids"] = new
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(DEFAULT_MODES))
    parser.add_argument("--files", nargs="+", help="test files to run (default: files that reach the estimator)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)

    modes = list(dict.fromkeys(["count", *args.modes]))
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        results: Dict[str, Dict[str, Any]] = {}
        targets = [f for f in (args.files or ["tests"]) if f not in SELF_TESTS]
        if not targets:
            # An empty target list would make pytest fall back to its
            # configured default discovery, measuring something not asked for.
            print("No target left to measure: the probe's own tests are never measured.", file=sys.stderr)
            return 2
        try:
            results["count"] = run_mode("count", targets, workdir)
            if not args.files:
                targets = sorted({node.split("::", 1)[0] for node in results["count"]["reached"]})
                if not targets:
                    print("No test reached the estimator.", file=sys.stderr)
                    return 1
                results["count"] = run_mode("count", targets, workdir)
            for mode in modes[1:]:
                results[mode] = run_mode(mode, targets, workdir)
        except IncompleteRun as exc:
            print(f"Incomplete run: {exc}", file=sys.stderr)
            return 2

    report = compare(results)
    report["reach"] = {"tests": len(results["count"]["reached"]), "files": len(targets)}
    report["summaries"] = {m: r["summary"] for m, r in results.items()}
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"Reach: {report['reach']['tests']} tests in {report['reach']['files']} files")
    for mode, summary in report["summaries"].items():
        extra = "" if mode == "count" else f"  (new failures: {report['new_failures'][mode]})"
        print(f"  {mode:15s} {summary}{extra}")
    if "state_advance_only" in report:
        print(f"State-advance only (fail frozen, pass with state advancing): {len(report['state_advance_only'])}")
    for family in ("freeze", "out"):
        key = f"{family}_verdict_sensitive"
        if key in report:
            print(f"Verdict-sensitive under {family}_*: {len(report[key])}")
            for node in report[key]:
                print(f"    {node}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
