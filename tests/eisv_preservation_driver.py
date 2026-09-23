"""Deterministic driver for the EISV instrument-preservation harness.

Pins what the live EISV estimator produces for fixed check-in sequences, so a
later change that claims to leave the instrument untouched (the observational
checkpoint seam in ``docs/proposals/active/eisv-core-boundary-v0.md``) can be shown to
change nothing. It is a regression pin, not a statement that these values are
correct: agreement with the golden file means "the same as the code it was
recorded from", never "valid".

Determinism rests on two things, both recorded here rather than assumed:

* every wall-clock read the estimator path makes is replaced by one injected
  clock (``CLOCK_SOURCES``). Without that, real latency between check-ins moves
  the dual-log continuity inputs, and two identical runs diverge by the second
  check-in;
* the tool-usage window is fixed, because the live tracker reads process-global
  call history;
* fleet calibration starts empty. The live checker loads
  ``data/calibration_state.json`` from the checkout, so a developer machine with
  a local state file could otherwise record a different trajectory.

``tests/test_eisv_instrument_preservation.py`` checks that ``CLOCK_SOURCES``
still names every clock read in these modules, so a new one cannot silently
break the pin.

Run as a module to print the trajectories as JSON (the test does this in a
subprocess so no earlier test in the session can leak state into it)::

    python -m tests.eisv_preservation_driver

Regenerating the golden file changes the pinned instrument. Before the
registered 2026-12-01 read's preservation horizon that needs the operator
decision recorded in ``docs/proposals/active/eisv-core-boundary-v0.md`` §12.1; after
it, do it only with a change that is meant to alter EISV behaviour, and say so::

    python -m tests.eisv_preservation_driver --write-golden
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = REPO_ROOT / "tests" / "fixtures" / "eisv_preservation" / "estimator_trajectories_v0.json"
GOLDEN_SCHEMA = "unitares.eisv_preservation.estimator_trajectories.v0"

# Float precision kept in the golden file. Discrete fields compare exactly; this
# only absorbs last-digit differences between interpreter builds.
FLOAT_DIGITS = 8

# Every module on the estimator path that reads a clock, the module attribute it
# reads it through, and whether that attribute is the ``datetime`` class or a
# ``time`` module. The injected clock replaces each one. The list is checked at
# runtime: tests/test_eisv_instrument_preservation.py traces every real clock
# call made from repository code while the scenarios run.
CLOCK_SOURCES = (
    ("src.governance_monitor", "datetime", "datetime"),
    ("src.dual_log.continuity", "datetime", "datetime"),
    ("src.dual_log.operational", "datetime", "datetime"),
    ("src.dual_log.reflective", "datetime", "datetime"),
    ("src.dual_log.restorative", "datetime", "datetime"),
    ("governance_core.ethical_drift", "datetime", "datetime"),
    # Gates trajectory calibration recording on >10 s between check-ins.
    ("src.monitor_calibration", "_time", "time"),
    # Prediction registry TTLs and timestamps.
    ("src.monitor_prediction", "_time", "time"),
    ("src.monitor_prediction", "datetime", "datetime"),
    # Result timestamp.
    ("src.monitor_result", "datetime", "datetime"),
)

# Real clock reads that remain during a run, deliberately not injected, keyed by
# (module, function name) as the runtime trace reports them, with the reason.
UNINJECTED_CLOCK_READS = {
    ("src.behavioral_state", "monotonic"): (
        "stamps BehavioralEISV.last_update_time; no computed field read by the "
        "pinned outputs depends on it"
    ),
    ("src.audit_log", "now"): (
        "timestamps audit entries; the driver redirects the audit log to a "
        "temporary file and nothing on the estimator path reads it back"
    ),
    ("src.calibration", "monotonic"): (
        "debounces the calibration JSON snapshot write; the driver's checker "
        "writes only to a temporary file and reads its in-memory state"
    ),
    ("src.drift_telemetry", "now"): (
        "timestamps drift telemetry records; redirected to a temporary directory "
        "and not read back on the estimator path"
    ),
}

# Process-global state the estimator reads and writes outside the monitor
# object. Each is reset before a run so runs are independent; the golden file is
# produced in a fresh subprocess, where they start empty anyway.
PROCESS_GLOBAL_STATE = {
    ("governance_core.ethical_drift", "_baseline_cache"): (
        "AgentBaseline per agent_id, kept for the life of the process and shared "
        "by every monitor instance for that agent_id, including one created after "
        "an in-process restart"
    ),
}

START = _dt.datetime(2026, 1, 1, 12, 0, 0)


class _Clock:
    now = START


class _InjectedDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401 - mirrors datetime.now
        return _Clock.now if tz is None else _Clock.now.replace(tzinfo=tz)

    @classmethod
    def utcnow(cls):
        return _Clock.now


class _InjectedTime:
    """Stands in for a module imported as ``import time as _time``."""

    def __init__(self, real):
        self._real = real

    def _seconds(self) -> float:
        return (_Clock.now - START).total_seconds() + 1_000_000.0

    def monotonic(self) -> float:
        return self._seconds()

    def perf_counter(self) -> float:
        return self._seconds()

    def time(self) -> float:
        return _Clock.now.replace(tzinfo=_dt.timezone.utc).timestamp()

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def injected_environment() -> Iterator[None]:
    """Replace every clock source, fix the tool-usage window, empty calibration,
    and send the audit log and drift telemetry to a temporary directory."""
    import importlib
    import tempfile

    import src.calibration as calibration

    with contextlib.ExitStack() as stack:
        scratch = stack.enter_context(tempfile.TemporaryDirectory())
        empty_checker = calibration.CalibrationChecker(
            state_file=Path(scratch) / "calibration_state.json"
        )
        # JSON-only persistence into the scratch directory: the default postgres
        # backend would schedule database writes whenever an event loop is running.
        empty_checker._backend = "json"
        stack.enter_context(patch.object(calibration, "_calibration_checker_instance", empty_checker))

        import src.audit_log as audit_log
        import src.drift_telemetry as drift_telemetry

        stack.enter_context(
            patch.object(audit_log.audit_logger, "log_file", Path(scratch) / "audit_log.jsonl")
        )
        stack.enter_context(
            patch.object(
                drift_telemetry,
                "_telemetry",
                drift_telemetry.DriftTelemetry(data_dir=Path(scratch) / "telemetry"),
            )
        )
        for module_name, attribute, kind in CLOCK_SOURCES:
            module = importlib.import_module(module_name)
            replacement = (
                _InjectedDatetime if kind == "datetime" else _InjectedTime(getattr(module, attribute))
            )
            stack.enter_context(patch.object(module, attribute, replacement))
        stack.enter_context(
            patch(
                "src.tool_usage_tracker.ToolUsageTracker.get_usage_stats",
                return_value={"total_calls": 0, "unique_tools": 0, "tools": {}},
            )
        )
        yield


def _round(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int,)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return repr(value)
        return round(value, FLOAT_DIGITS)
    try:
        return round(float(value), FLOAT_DIGITS)
    except (TypeError, ValueError):
        return str(value)


def _round_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _round_tree(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_round_tree(item) for item in value]
    if isinstance(value, str):
        return value
    return _round(value)


def _capture(monitor, result: Dict[str, Any]) -> Dict[str, Any]:
    decision = result.get("decision") or {}
    metrics = result.get("metrics") or {}
    behavioral = monitor._behavioral_state
    continuity = monitor._last_continuity_metrics
    return {
        "action": decision.get("action"),
        "sub_action": decision.get("sub_action"),
        "verdict": metrics.get("verdict"),
        "status": result.get("status"),
        "regime": metrics.get("regime"),
        "risk_score": _round(metrics.get("risk_score")),
        "E": _round(metrics.get("E")),
        "I": _round(metrics.get("I")),
        "S": _round(metrics.get("S")),
        "V": _round(metrics.get("V")),
        "coherence": _round(metrics.get("coherence")),
        "phi": _round(metrics.get("phi")),
        # Calibration recording, gated on elapsed time between check-ins.
        "trajectory_validation": _round_tree(result.get("trajectory_validation")),
        "confidence_source": (result.get("confidence_reliability") or {}).get("source"),
        "behavioral": {
            "update_count": getattr(behavioral, "update_count", None),
            "is_baselined": bool(getattr(behavioral, "is_baselined", False)),
            "obs_source": getattr(monitor, "_behavioral_obs_source", None),
        },
        "continuity": {
            "derived_complexity": _round(getattr(continuity, "derived_complexity", None)),
            "complexity_divergence": _round(getattr(continuity, "complexity_divergence", None)),
            "E_input": _round(getattr(continuity, "E_input", None)),
            "I_input": _round(getattr(continuity, "I_input", None)),
            "S_input": _round(getattr(continuity, "S_input", None)),
        },
    }


def _new_monitor(agent_id: str):
    from src.governance_monitor import UNITARESMonitor

    return UNITARESMonitor(agent_id, load_state=False)


def _step(monitor, *, seconds: float, text: str, complexity: float, confidence, extra=None):
    _Clock.now = _Clock.now + _dt.timedelta(seconds=seconds)
    agent_state = {"response_text": text, "complexity": complexity}
    if extra:
        agent_state.update(extra)
    result = monitor.process_update(agent_state, confidence=confidence, task_type="mixed")
    return _capture(monitor, result)


def scenario_warmup_to_self_relative() -> List[Dict[str, Any]]:
    """32 check-ins through all warmup stages, a confidence/complexity shift, and a gap."""
    monitor = _new_monitor("preservation-warmup")
    steps = []
    for i in range(32):
        steps.append(
            _step(
                monitor,
                seconds=400 if i == 20 else 30,
                text=f"step {i}: edited two files and ran the unit tests",
                complexity=0.4 if i < 12 else 0.9,
                confidence=0.8 if i < 12 else 0.3,
            )
        )
    return steps


def scenario_high_risk_first_checkins() -> List[Dict[str, Any]]:
    """Terse, high-complexity, low-confidence reports from the first check-in."""
    monitor = _new_monitor("preservation-high-risk")
    return [
        _step(monitor, seconds=30, text="x", complexity=0.9, confidence=0.2)
        for _ in range(6)
    ]


def scenario_saturating_gap() -> List[Dict[str, Any]]:
    """A gap far beyond the cadence band: dt saturation and gap-recovery arming."""
    monitor = _new_monitor("preservation-gap")
    steps = []
    for i, seconds in enumerate([30, 30, 30, 20 * 3600, 30, 30]):
        steps.append(
            _step(
                monitor,
                seconds=seconds,
                text=f"step {i}: reviewed a pull request and left comments",
                complexity=0.5,
                confidence=0.7,
            )
        )
    return steps


def scenario_embodied_sensor() -> List[Dict[str, Any]]:
    """Externally supplied sensor EISV feeding the behavioral path."""
    monitor = _new_monitor("preservation-embodied")
    steps = []
    for i in range(8):
        sensor = {"E": 0.45 + 0.01 * i, "I": 0.6, "S": 0.25 - 0.005 * i}
        steps.append(
            _step(
                monitor,
                seconds=60,
                text=f"sensor cycle {i}",
                complexity=0.3,
                confidence=None,
                extra={"sensor_eisv": sensor, "sensor_eisv_source": "physical"},
            )
        )
    return steps


def scenario_behavioral_source_sensor() -> List[Dict[str, Any]]:
    """Sensor EISV tagged ``behavioral``, the shape the check-in path submits for
    non-embodied agents (``phases.py`` builds it from histories and tool usage).
    The values are fixed here, so this pins how the monitor consumes that
    submission, not how the check-in path computes it."""
    monitor = _new_monitor("preservation-behavioral-sensor")
    steps = []
    for i in range(30):
        sensor = {"E": 0.70 - 0.004 * i, "I": 0.72, "S": 0.20 + 0.003 * i}
        steps.append(
            _step(
                monitor,
                seconds=45,
                text=f"step {i}: refactored a module and re-ran the suite",
                complexity=0.5,
                confidence=0.75,
                extra={"sensor_eisv": sensor, "sensor_eisv_source": "behavioral"},
            )
        )
    return steps


def scenario_behavioral_state_roundtrip() -> List[Dict[str, Any]]:
    """Behavioral state serialized and restored onto a new monitor object.

    This is NOT a restart. It pins the ``to_dict_for_persistence`` /
    ``BehavioralEISV.from_dict`` round trip only, in-process, so the ODE state is
    not restored, the elapsed time is 30 s rather than a downtime, and the
    process-global ethical-drift baseline carries over. Real restarts rebuild
    the monitor from the JSON snapshot or from database rows and are not pinned.
    """
    from src.behavioral_state import BehavioralEISV

    first = _new_monitor("preservation-restore")
    steps = []
    for i in range(26):
        steps.append(
            _step(first, seconds=30, text=f"before restart {i}: ran tests", complexity=0.5, confidence=0.7)
        )
    snapshot = json.loads(json.dumps(first._behavioral_state.to_dict_for_persistence()))
    second = _new_monitor("preservation-restore")
    second._behavioral_state = BehavioralEISV.from_dict(snapshot)
    for i in range(6):
        steps.append(
            _step(second, seconds=30, text=f"after restart {i}: ran tests", complexity=0.5, confidence=0.7)
        )
    return steps


SCENARIOS = {
    "warmup_to_self_relative": scenario_warmup_to_self_relative,
    "high_risk_first_checkins": scenario_high_risk_first_checkins,
    "saturating_gap": scenario_saturating_gap,
    "embodied_sensor": scenario_embodied_sensor,
    "behavioral_source_sensor": scenario_behavioral_source_sensor,
    "behavioral_state_roundtrip": scenario_behavioral_state_roundtrip,
}


def _reset_process_global_state() -> None:
    import importlib

    for module_name, attribute in PROCESS_GLOBAL_STATE:
        getattr(importlib.import_module(module_name), attribute).clear()


# Environment prefixes that change estimator configuration. The golden file pins
# the shipped defaults, so these are removed before any configuration is read.
CONFIG_ENV_PREFIXES = ("UNITARES_", "GOVERNANCE_")


def strip_config_env(environ: Dict[str, str]) -> Dict[str, str]:
    return {key: value for key, value in environ.items() if not key.startswith(CONFIG_ENV_PREFIXES)}


def run_all() -> Dict[str, Any]:
    _reset_process_global_state()
    trajectories = {}
    with injected_environment():
        for name, scenario in SCENARIOS.items():
            _Clock.now = START
            trajectories[name] = scenario()
    return {"schema": GOLDEN_SCHEMA, "float_digits": FLOAT_DIGITS, "trajectories": trajectories}


def comparable(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The part of a payload the pin compares; provenance is informational."""
    return {key: value for key, value in payload.items() if key != "provenance"}


def _provenance() -> Dict[str, Any]:
    import platform
    import subprocess

    import numpy

    def _git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except Exception:
            return "unknown"

    return {
        "recorded_from_commit": _git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(_git("status", "--porcelain", "--", "src", "governance_core", "config")),
        "python": platform.python_version(),
        "numpy": numpy.__version__,
        "platform": platform.system(),
    }


def first_difference(expected: Any, actual: Any, path: str = "$") -> str | None:
    """Return a readable path to the first difference, or None when equal."""
    if type(expected) is not type(actual):
        return f"{path}: type {type(expected).__name__} != {type(actual).__name__} ({expected!r} vs {actual!r})"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                return f"{path}.{key}: unexpected key (value {actual[key]!r})"
            if key not in actual:
                return f"{path}.{key}: missing key (expected {expected[key]!r})"
            diff = first_difference(expected[key], actual[key], f"{path}.{key}")
            if diff:
                return diff
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: length {len(expected)} != {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            diff = first_difference(left, right, f"{path}[{index}]")
            if diff:
                return diff
        return None
    if expected != actual:
        return f"{path}: expected {expected!r}, got {actual!r}"
    return None


def main(argv: List[str]) -> int:
    import logging

    logging.disable(logging.CRITICAL)
    for key in list(os.environ):
        if key.startswith(CONFIG_ENV_PREFIXES):
            del os.environ[key]
    payload = run_all()
    if "--write-golden" in argv:
        payload["provenance"] = _provenance()
    text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
    if "--write-golden" in argv:
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {GOLDEN_PATH.relative_to(REPO_ROOT)}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
