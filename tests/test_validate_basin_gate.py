"""The basin-gate validator must be able to FAIL.

`validate_basin_gate.py` certifies that the #689 basin gate stops the #686
false-pause class without masking genuine danger. It printed
"PASS — acceptance criteria met" with the gate deleted: every assertion was
already satisfied by the `before` column, which the script computes precisely to
represent the pre-gate world. The two rows where the gate changed a verdict were
the two carrying no assertion.

That is the antipattern from `docs/operations/positive-control-validity-2026-08-23.md`
— an instrument whose verdict cannot distinguish treatment from no treatment.
These tests pin the repair by driving both worlds.
"""

from __future__ import annotations

import src.behavioral_assessment as ba
import scripts.analysis.validate_basin_gate as validator


def _delete_the_gate(monkeypatch):
    """Replace the basin gate with the constant-1.0 no-op.

    Arithmetically identical to removing the `* gate[...]` multipliers, i.e. the
    world in which #689 was never implemented.
    """
    monkeypatch.setattr(
        ba,
        "_basin_health_gate",
        lambda state: {"low_E": 1.0, "low_I": 1.0, "high_S": 1.0, "high_V": 1.0},
    )


def test_sweep_passes_with_the_gate_present():
    assert validator.run_sweep() is True


def test_sweep_fails_when_the_gate_is_deleted(monkeypatch):
    """The test this file exists for. Before the repair this returned True."""
    _delete_the_gate(monkeypatch)
    assert validator.run_sweep() is False


def test_trace_cases_pass_with_the_gate_present():
    assert validator.run_trace_cases() is True


def test_at_least_one_case_asserts_on_the_treatment_effect():
    """A `de_escalates` case is the only kind the gate's absence can fail.

    Guards against the expectations silently reverting to `none`, which is how
    the effect became unasserted in the first place.
    """
    import inspect

    source = inspect.getsource(validator.run_sweep)
    assert source.count('"de_escalates"') >= 2


def test_live_arm_reports_a_skip_as_a_skip(monkeypatch):
    """A skipped live arm must not read as a pass.

    Every early exit returned a bare True, which `main` ANDed into an
    unqualified "PASS — acceptance criteria met".
    """
    import asyncio

    monkeypatch.setattr(validator, "_build_from_stats", validator._build_from_stats)
    ok, examined, skip_reason = asyncio.run(
        validator.run_live("postgresql://127.0.0.1:1/nope", 10)
    )
    assert ok is True          # a skip is not a failure
    assert examined == 0
    assert skip_reason is not None   # ...but it is not a pass either


def test_main_qualifies_its_verdict_when_the_live_arm_skips(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["validate_basin_gate.py", "--db"])
    rc = validator.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "live arm SKIPPED" in out
    assert "PASS — acceptance criteria met" not in out


def test_main_gives_an_unqualified_pass_only_for_the_synthetic_arms(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["validate_basin_gate.py"])
    rc = validator.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS — acceptance criteria met" in out
