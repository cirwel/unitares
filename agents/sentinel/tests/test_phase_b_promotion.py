"""Tests for ``agents/sentinel/phase_b_promotion.py``.

Stubs the HTTP evaluator call so transition-detection logic can be tested
without a running server. Server-side Phase-B evaluation is exercised via
``tests/test_lease_plane_phase_b_evaluator.py`` and ``tests/test_resident_db_boundaries.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agents.sentinel import phase_b_promotion as pbp


def _report(
    surface_kind: str,
    *,
    promotable: bool,
    statuses: dict[int, str],
) -> dict:
    """Build a mock evaluator JSON report."""
    return {
        "surface_kind": surface_kind,
        "window_days": 14,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "promotable": promotable,
        "criteria": [
            {
                "number": n,
                "name": f"criterion_{n}",
                "status": status,
                "detail": f"mock detail {n}",
                "measured": {},
            }
            for n, status in sorted(statuses.items())
        ],
    }


def _all_fail():
    return {1: "FAIL", 2: "PASS", 3: "FAIL", 4: "NOT_YET_EVALUABLE",
            5: "NOT_APPLICABLE", 6: "NOT_YET_EVALUABLE"}


def _all_pass():
    return {1: "PASS", 2: "PASS", 3: "PASS", 4: "PASS",
            5: "NOT_APPLICABLE", 6: "PASS"}


def test_first_observation_is_silent_when_not_promotable(monkeypatch, tmp_path):
    """Baseline observation against an empty cache: no transition emitted."""
    monkeypatch.setattr(
        pbp,
        "_run_evaluator",
        lambda surface_kind, *, api_url=None, api_token=None: _report(
            surface_kind, promotable=False, statuses=_all_fail()
        ),
    )
    state_path = tmp_path / "verdict.json"
    transitions = pbp.detect_transitions(["dialectic"], state_path=state_path)
    assert transitions == []
    # State must still be persisted so the next call has a baseline.
    saved = json.loads(state_path.read_text())
    assert "dialectic" in saved
    assert saved["dialectic"]["promotable"] is False


def test_first_observation_emits_when_already_promotable(monkeypatch, tmp_path):
    """If we discover a surface is already PROMOTABLE on first observation,
    that's worth surfacing — operator may have missed the transition."""
    monkeypatch.setattr(
        pbp,
        "_run_evaluator",
        lambda surface_kind, *, api_url=None, api_token=None: _report(
            surface_kind, promotable=True, statuses=_all_pass()
        ),
    )
    state_path = tmp_path / "verdict.json"
    transitions = pbp.detect_transitions(["dialectic"], state_path=state_path)
    assert len(transitions) == 1
    assert transitions[0].surface_kind == "dialectic"
    assert transitions[0].promotable_now is True
    assert "PROMOTABLE" in transitions[0].summary


def test_steady_state_emits_nothing(monkeypatch, tmp_path):
    """Identical evaluator output across two calls → no transition."""
    monkeypatch.setattr(
        pbp,
        "_run_evaluator",
        lambda surface_kind, *, api_url=None, api_token=None: _report(
            surface_kind, promotable=False, statuses=_all_fail()
        ),
    )
    state_path = tmp_path / "verdict.json"
    pbp.detect_transitions(["dialectic"], state_path=state_path)
    transitions = pbp.detect_transitions(["dialectic"], state_path=state_path)
    assert transitions == []


def test_single_criterion_flip_emits_transition(monkeypatch, tmp_path):
    """Criterion 3 flipping FAIL→PASS produces a precise transition record."""
    state_path = tmp_path / "verdict.json"

    statuses = _all_fail()
    monkeypatch.setattr(
        pbp,
        "_run_evaluator",
        lambda surface_kind, *, api_url=None, api_token=None: _report(
            surface_kind, promotable=False, statuses=statuses
        ),
    )
    pbp.detect_transitions(["dialectic"], state_path=state_path)

    # Now criterion 3 passes
    statuses[3] = "PASS"
    transitions = pbp.detect_transitions(["dialectic"], state_path=state_path)
    assert len(transitions) == 1
    t = transitions[0]
    assert t.promotable_now is False  # other criteria still failing
    assert t.promotable_before is False
    assert len(t.criteria) == 1
    c = t.criteria[0]
    assert c.number == 3
    assert c.previous_status == "FAIL"
    assert c.current_status == "PASS"
    assert "§6.1.3" in t.summary
    assert "FAIL → PASS" in t.summary


def test_promotable_flip_marks_high_severity_summary(monkeypatch, tmp_path):
    """Flipping to PROMOTABLE produces the canonical PROMOTABLE summary line."""
    state_path = tmp_path / "verdict.json"

    statuses = {1: "PASS", 2: "PASS", 3: "FAIL", 4: "PASS",
                5: "NOT_APPLICABLE", 6: "PASS"}
    monkeypatch.setattr(
        pbp,
        "_run_evaluator",
        lambda surface_kind, *, api_url=None, api_token=None: _report(
            surface_kind, promotable=False, statuses=statuses
        ),
    )
    pbp.detect_transitions(["dialectic"], state_path=state_path)

    statuses[3] = "PASS"  # the last failing criterion clears
    monkeypatch.setattr(
        pbp,
        "_run_evaluator",
        lambda surface_kind, *, api_url=None, api_token=None: _report(
            surface_kind, promotable=True, statuses=statuses
        ),
    )
    transitions = pbp.detect_transitions(["dialectic"], state_path=state_path)
    assert len(transitions) == 1
    t = transitions[0]
    assert t.promotable_now is True
    assert t.promotable_before is False
    assert "PROMOTABLE" in t.summary


def test_regression_marks_distinct_summary(monkeypatch, tmp_path):
    """A surface that was promotable becoming not-promotable produces a
    REGRESSED summary, distinct from the PROMOTABLE direction."""
    state_path = tmp_path / "verdict.json"

    statuses = _all_pass()
    monkeypatch.setattr(
        pbp,
        "_run_evaluator",
        lambda surface_kind, *, api_url=None, api_token=None: _report(
            surface_kind, promotable=True, statuses=statuses
        ),
    )
    pbp.detect_transitions(["dialectic"], state_path=state_path)

    statuses[2] = "FAIL"  # uptime regression
    monkeypatch.setattr(
        pbp,
        "_run_evaluator",
        lambda surface_kind, *, api_url=None, api_token=None: _report(
            surface_kind, promotable=False, statuses=statuses
        ),
    )
    transitions = pbp.detect_transitions(["dialectic"], state_path=state_path)
    assert len(transitions) == 1
    assert "REGRESSED" in transitions[0].summary


def test_multiple_surface_kinds_each_get_own_state(monkeypatch, tmp_path):
    """Independent transitions for distinct surface_kinds in one call."""
    state_path = tmp_path / "verdict.json"

    def runner(surface_kind, *, api_url=None, api_token=None):
        if surface_kind == "dialectic":
            return _report(surface_kind, promotable=False, statuses=_all_fail())
        return _report(surface_kind, promotable=True, statuses=_all_pass())

    monkeypatch.setattr(pbp, "_run_evaluator", runner)
    transitions = pbp.detect_transitions(
        ["dialectic", "resident"], state_path=state_path,
    )
    # dialectic baseline: silent (not promotable on first obs).
    # resident first-observation-promotable: emitted.
    assert len(transitions) == 1
    assert transitions[0].surface_kind == "resident"

    saved = json.loads(state_path.read_text())
    assert set(saved.keys()) == {"dialectic", "resident"}


def test_duplicate_surface_kinds_deduped(monkeypatch, tmp_path):
    """Same surface_kind passed twice → evaluator runs once."""
    call_count = 0

    def runner(surface_kind, *, api_url=None, api_token=None):
        nonlocal call_count
        call_count += 1
        return _report(surface_kind, promotable=True, statuses=_all_pass())

    monkeypatch.setattr(pbp, "_run_evaluator", runner)
    pbp.detect_transitions(
        ["dialectic", "dialectic"], state_path=tmp_path / "v.json",
    )
    assert call_count == 1


def test_detect_transitions_forwards_api_url_and_token(monkeypatch, tmp_path):
    """Sentinel passes `api_url` (and optional `api_token`) through to `_run_evaluator`."""
    observed: list[tuple[str, str | None, str | None]] = []

    def runner(surface_kind, *, api_url=None, api_token=None):
        observed.append((surface_kind, api_url, api_token))
        return _report(surface_kind, promotable=True, statuses=_all_pass())

    monkeypatch.setattr(pbp, "_run_evaluator", runner)
    transitions = pbp.detect_transitions(
        ["dialectic"],
        state_path=tmp_path / "v.json",
        api_url="http://core-host:8767",
        api_token="tok-123",
    )
    assert len(transitions) == 1
    assert observed == [("dialectic", "http://core-host:8767", "tok-123")]


def test_evaluator_failure_raises_typed_error(monkeypatch, tmp_path):
    """Evaluator HTTP failure is wrapped in PhaseBEvaluatorError so
    the Sentinel caller can log-and-swallow without losing context."""

    def boom(surface_kind, *, api_url=None, api_token=None):
        raise RuntimeError("http 503 unreachable")

    monkeypatch.setattr(pbp, "_run_evaluator", boom)
    with pytest.raises(pbp.PhaseBEvaluatorError) as excinfo:
        pbp.detect_transitions(["dialectic"], state_path=tmp_path / "v.json")
    assert excinfo.value.surface_kind == "dialectic"
    assert "http 503 unreachable" in excinfo.value.reason


def test_empty_input_short_circuits(monkeypatch, tmp_path):
    """No surface_kinds → no evaluator invocations, no state written."""
    monkeypatch.setattr(
        pbp, "_run_evaluator",
        lambda *a, **kw: pytest.fail("evaluator must not run on empty input"),
    )
    transitions = pbp.detect_transitions([], state_path=tmp_path / "v.json")
    assert transitions == []
    assert not (tmp_path / "v.json").exists()


def test_corrupted_state_file_treated_as_empty(monkeypatch, tmp_path):
    """A garbage state file shouldn't crash the cycle — treat as fresh observation."""
    state_path = tmp_path / "v.json"
    state_path.write_text("not json {{{")
    monkeypatch.setattr(
        pbp,
        "_run_evaluator",
        lambda surface_kind, *, api_url=None, api_token=None: _report(
            surface_kind, promotable=False, statuses=_all_fail()
        ),
    )
    transitions = pbp.detect_transitions(["dialectic"], state_path=state_path)
    assert transitions == []
    # State was rewritten with valid JSON.
    saved = json.loads(state_path.read_text())
    assert "dialectic" in saved
