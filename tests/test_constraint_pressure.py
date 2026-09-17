"""Synthetic invariants for the replay-only constraint-pressure primitive."""

import json
import math

import pytest

from src.constraint_pressure import (
    SCHEMA_VERSION,
    ConstraintPressureConfig,
    ConstraintPressureState,
    advance_constraint_pressure,
)


def _config(**overrides) -> ConstraintPressureConfig:
    values = {
        "constraint_id": "synthetic.test.v0",
        "accumulation_rate": 0.4,
        "leak_rate": 0.2,
        "max_pressure": 10.0,
    }
    values.update(overrides)
    return ConstraintPressureConfig(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("accumulation_rate", -0.1),
        ("accumulation_rate", math.inf),
        ("leak_rate", -0.1),
        ("leak_rate", math.nan),
        ("max_pressure", 0.0),
    ],
)
def test_config_rejects_invalid_values(field, value):
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_constant_residual_is_interval_partition_invariant():
    config = _config()
    initial = ConstraintPressureState(pressure=0.7)

    whole = advance_constraint_pressure(
        initial, config, residual=0.6, dt_seconds=10.0
    ).state
    split = initial
    for _ in range(10):
        split = advance_constraint_pressure(
            split, config, residual=0.6, dt_seconds=1.0
        ).state

    assert split.pressure == pytest.approx(whole.pressure, abs=1e-12)
    assert split.observed_seconds == whole.observed_seconds == 10.0


def test_checkpoint_restore_matches_uninterrupted_replay():
    config = _config()
    state = advance_constraint_pressure(
        ConstraintPressureState(), config, residual=0.5, dt_seconds=3.0
    ).state
    checkpoint = json.loads(json.dumps(state.to_dict(), sort_keys=True))
    restored = ConstraintPressureState.from_dict(checkpoint)

    resumed = advance_constraint_pressure(
        restored, config, residual=0.2, dt_seconds=7.0
    ).state
    uninterrupted = advance_constraint_pressure(
        state, config, residual=0.2, dt_seconds=7.0
    ).state

    assert resumed == uninterrupted


def test_missing_observation_holds_pressure_and_time():
    config = _config()
    state = ConstraintPressureState(pressure=0.8, observed_seconds=12.0)

    transition = advance_constraint_pressure(
        state, config, residual=None, dt_seconds=10_000.0
    )

    assert transition.state is state
    assert transition.observed is False
    assert transition.state.pressure == 0.8
    assert transition.state.observed_seconds == 12.0


def test_independent_resolution_clears_pressure():
    transition = advance_constraint_pressure(
        ConstraintPressureState(pressure=0.8, observed_seconds=12.0),
        _config(),
        residual=None,
        dt_seconds=1.0,
        resolved=True,
    )

    assert transition.resolved is True
    assert transition.state.pressure == 0.0
    assert transition.state.observed_seconds == 12.0


def test_resolution_and_residual_are_ambiguous():
    with pytest.raises(ValueError, match="cannot be supplied together"):
        advance_constraint_pressure(
            ConstraintPressureState(),
            _config(),
            residual=0.2,
            dt_seconds=1.0,
            resolved=True,
        )


def test_zero_accumulation_rate_produces_decay_only():
    transition = advance_constraint_pressure(
        ConstraintPressureState(pressure=1.0),
        _config(accumulation_rate=0.0),
        residual=100.0,
        dt_seconds=5.0,
    )

    assert transition.state.pressure == pytest.approx(math.exp(-1.0))


def test_zero_leak_accumulates_monotonically_until_cap():
    config = _config(leak_rate=0.0, max_pressure=1.0)
    state = ConstraintPressureState(pressure=0.2)
    pressures = []
    last_transition = None
    for _ in range(10):
        last_transition = advance_constraint_pressure(
            state, config, residual=0.5, dt_seconds=1.0
        )
        state = last_transition.state
        pressures.append(state.pressure)

    assert pressures == sorted(pressures)
    assert pressures[-1] == 1.0
    assert last_transition is not None and last_transition.saturated is True


@pytest.mark.parametrize("value", [-1.0, math.inf, math.nan])
def test_invalid_residual_is_rejected(value):
    with pytest.raises(ValueError):
        advance_constraint_pressure(
            ConstraintPressureState(),
            _config(),
            residual=value,
            dt_seconds=1.0,
        )


def test_manifest_digest_is_stable_and_parameter_sensitive():
    config = _config()
    equivalent = _config()
    changed = _config(leak_rate=0.3)

    assert config.manifest()["schema_version"] == SCHEMA_VERSION
    assert config.manifest_digest == equivalent.manifest_digest
    assert config.manifest_digest != changed.manifest_digest
    assert len(config.manifest_digest) == 64
