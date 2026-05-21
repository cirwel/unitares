"""
Phase B promotion-eligibility transition detector for Sentinel.

Hooks onto the existing `conflict_held_by_other` batched alarm cycle: when a
conflict batch fires for a surface_kind, re-run
``scripts/lease_plane/evaluate_phase_b_promotion.py`` for that surface_kind
and emit a transition finding only when one or more §6.1 criteria change
status vs. the last recorded verdict (or when overall ``promotable`` flips).

Spec: §6.1 / §6.2 ordering.

Design notes:
  * Event-triggered, not cron — piggybacks on Sentinel's existing
    `_emit_forced_release_alarms` cycle, no separate poll loop.
  * On-disk state at ``data/lease_plane/last_phase_b_verdict.json`` so a
    Sentinel restart doesn't re-emit the same transition.
  * Subprocess invocation of the evaluator (rather than importing) keeps
    Sentinel insulated from psycopg2 import gymnastics in test environments
    that stub psycopg2 (see tests/test_calibrate_class_conditional.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EVALUATOR_SCRIPT = REPO_ROOT / "scripts" / "lease_plane" / "evaluate_phase_b_promotion.py"
DEFAULT_STATE_PATH = REPO_ROOT / "data" / "lease_plane" / "last_phase_b_verdict.json"


@dataclass
class CriterionTransition:
    number: int
    name: str
    previous_status: str | None  # None on first observation
    current_status: str
    detail: str


@dataclass
class PhaseBTransition:
    surface_kind: str
    promotable_now: bool
    promotable_before: bool | None  # None on first observation
    criteria: list[CriterionTransition] = field(default_factory=list)
    summary: str = ""

    @property
    def is_meaningful(self) -> bool:
        """True iff any criterion changed or `promotable` flipped.

        First-observation runs (previous == None) emit only when the surface
        is already promotable — a useful signal even without a prior comparison.
        Otherwise, a baseline observation is silent.
        """
        if self.promotable_before is None:
            return self.promotable_now
        if self.promotable_now != self.promotable_before:
            return True
        return any(c.previous_status != c.current_status for c in self.criteria)


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _run_evaluator(surface_kind: str, *, db_url: str | None = None) -> dict[str, Any]:
    """Invoke the evaluator CLI and return its parsed JSON report.

    Raises CalledProcessError on exit codes 2 (DB error) or 3 (unknown
    surface_kind). Exit codes 0 (promotable) and 1 (not-promotable) both
    yield a valid report; other codes are propagated as errors.
    """
    args = [sys.executable, str(EVALUATOR_SCRIPT), surface_kind, "--json"]
    if db_url is not None:
        args.extend(["--db-url", db_url])
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"evaluator exited {result.returncode} for surface_kind='{surface_kind}': "
            f"{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def _diff_against_previous(
    surface_kind: str, current: dict[str, Any], previous: dict[str, Any] | None
) -> PhaseBTransition:
    """Build a PhaseBTransition by comparing current evaluator output to
    the prior recorded verdict (or None on first observation)."""
    previous_promotable = previous.get("promotable") if previous else None
    previous_statuses = (
        {int(k): v for k, v in previous.get("criteria_status", {}).items()}
        if previous
        else {}
    )

    transitions: list[CriterionTransition] = []
    for c in current["criteria"]:
        prior = previous_statuses.get(c["number"])
        if prior != c["status"]:
            transitions.append(
                CriterionTransition(
                    number=c["number"],
                    name=c["name"],
                    previous_status=prior,
                    current_status=c["status"],
                    detail=c["detail"],
                )
            )

    transition = PhaseBTransition(
        surface_kind=surface_kind,
        promotable_now=current["promotable"],
        promotable_before=previous_promotable,
        criteria=transitions,
    )
    transition.summary = _format_summary(transition)
    return transition


def _format_summary(transition: PhaseBTransition) -> str:
    if transition.promotable_now and transition.promotable_before is not True:
        verdict = (
            f"[lease-plane] {transition.surface_kind}: "
            f"PROMOTABLE — all §6.1 criteria PASS or N/A"
        )
    elif transition.promotable_before is True and not transition.promotable_now:
        verdict = (
            f"[lease-plane] {transition.surface_kind}: "
            f"REGRESSED — was PROMOTABLE, now NOT"
        )
    else:
        verdict = f"[lease-plane] {transition.surface_kind}:"

    bullets = [
        f"§6.1.{c.number} ({c.name}): "
        f"{c.previous_status or 'unobserved'} → {c.current_status}"
        for c in transition.criteria
    ]
    if not bullets:
        return verdict
    return verdict + " " + "; ".join(bullets)


def _to_state_record(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "evaluated_at": report["evaluated_at"],
        "promotable": report["promotable"],
        "criteria_status": {str(c["number"]): c["status"] for c in report["criteria"]},
    }


def detect_transitions(
    surface_kinds: list[str],
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    db_url: str | None = None,
) -> list[PhaseBTransition]:
    """Run the evaluator for each surface_kind and return meaningful
    transitions vs. the on-disk verdict cache. Updates the cache as a
    side-effect — repeated calls with no underlying change yield no
    transitions.

    Caller is responsible for emitting the transitions as findings; this
    function only detects them.
    """
    if not surface_kinds:
        return []
    state = _load_state(state_path)
    transitions: list[PhaseBTransition] = []

    for surface_kind in dict.fromkeys(surface_kinds):  # preserve order, dedupe
        try:
            report = _run_evaluator(surface_kind, db_url=db_url)
        except (subprocess.SubprocessError, RuntimeError, OSError) as e:
            # Per CLAUDE.md alarm-poll discipline: failures must not break
            # the Sentinel cycle. Surface as a log line via the caller.
            raise PhaseBEvaluatorError(surface_kind, str(e)) from e

        previous = state.get(surface_kind)
        transition = _diff_against_previous(surface_kind, report, previous)
        if transition.is_meaningful:
            transitions.append(transition)
        state[surface_kind] = _to_state_record(report)

    _save_state(state_path, state)
    return transitions


class PhaseBEvaluatorError(Exception):
    def __init__(self, surface_kind: str, reason: str):
        super().__init__(f"phase-B evaluator failed for surface_kind='{surface_kind}': {reason}")
        self.surface_kind = surface_kind
        self.reason = reason
