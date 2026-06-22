"""Trust-contract §7 provenance linting — applied to the live read surface.

`docs/trust-contract.md` §7 lists provenance linting as *"not built"*. This
suite ships it: `src.trust_contract_lint.lint_response` is run against the
real `get_governance_metrics_data` output across every verbosity, for both an
uninitialized and an initialized agent, and asserted clean. The self-tests at
the bottom prove the linter actually rejects the §6 violation classes — a lint
that never fires would be exactly the "appears more alive than it is" inertia
the contract guards against.

Companion to `tests/test_zero_observation_honesty.py`: that suite pins the
*specific* uninitialized shape; this one pins the *mechanical invariant* that
no endpoint emits a confident label in an ignorance state or an unlabeled
provenance-bearing field.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# Import-order anchor: settle the handler chain before runtime_queries is
# pulled (same cycle guard as test_zero_observation_honesty).
import src.mcp_handlers.core  # noqa: F401

from src.trust_contract_lint import (
    GOVERNANCE_METRICS_SPEC,
    LintSpec,
    ProvenanceViolation,
    assert_clean,
    is_ignorance_state,
    lint_response,
)


def _server_for(monitor):
    return SimpleNamespace(
        get_or_create_monitor=lambda aid: monitor,
        agent_metadata={},
    )


def _fresh_monitor(agent_id="test-lint-fresh"):
    from src.governance_monitor import UNITARESMonitor
    return UNITARESMonitor(agent_id, load_state=False)


@pytest.fixture(autouse=True)
def _no_db_hydration():
    with patch(
        "src.agent_monitor_state.hydrate_from_db_if_fresh",
        new=AsyncMock(return_value=False),
    ):
        yield


# ---------------------------------------------------------------------------
# Live-surface conformance: the shipping response must lint clean.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"lite": False},
    {"lite": True},
    {"verbosity": "standard"},
])
async def test_uninitialized_response_lints_clean(args):
    monitor = _fresh_monitor()
    from src.services.runtime_queries import get_governance_metrics_data
    data = await get_governance_metrics_data(
        "test-lint-fresh", args, server=_server_for(monitor)
    )
    assert is_ignorance_state(data), (
        "fresh agent must read as an ignorance state for rule B to apply"
    )
    assert_clean(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"lite": False},
    {"lite": True},
])
async def test_initialized_response_lints_clean(args):
    monitor = _fresh_monitor("test-lint-active")
    monitor.process_update({
        "response_text": "Real check-in so the interpretation surface is live.",
        "complexity": 0.5,
    })
    from src.services.runtime_queries import get_governance_metrics_data
    data = await get_governance_metrics_data(
        "test-lint-active", args, server=_server_for(monitor)
    )
    assert not is_ignorance_state(data)
    assert_clean(data)


# ---------------------------------------------------------------------------
# The linter is not inert: each rule must fire on its violation class.
# ---------------------------------------------------------------------------


def test_rule_b_flags_confident_label_in_ignorance_state():
    """§6 rows 1-3: a seed-derived label beside an uninitialized status."""
    bad = {
        "status": "uninitialized",
        "history_size": 0,
        "summary": "moderate | building_alone | high basin",  # the regressed shape
        "state": {"health": "moderate", "mode": "building_alone"},
    }
    violations = lint_response(bad)
    rules = {v.rule for v in violations}
    fields = {v.field for v in violations}
    assert "B" in rules
    assert {"summary", "state"} <= fields


def test_rule_b_passes_when_labels_are_ignorance_shaped():
    good = {
        "status": "⚪ uninitialized",
        "history_size": 0,
        "summary": "uninitialized | no observations yet",
        "state": {"status": "pending (first check-in required)"},
        "verdict": {"value": "uninitialized"},
    }
    # `verdict` is not in ASSESSMENT_FIELDS but `summary`/`state` are; both honest.
    assert lint_response(good) == []


def test_rule_a_flags_eisv_without_source():
    """§6 row 3 / §2: primary_eisv present, its source label dropped."""
    bad = {
        "status": "active",
        "primary_eisv": {"E": 0.4, "I": 0.6, "S": 0.1, "V": 0.0},
        # primary_eisv_source intentionally missing
    }
    violations = lint_response(bad)
    assert any(v.rule == "A" and v.field == "primary_eisv" for v in violations)


def test_rule_a_flags_saturation_without_inline_source():
    bad = {
        "status": "active",
        "saturation_diagnostics": {"sat_margin": -0.2, "dynamics_mode": "linear"},
    }
    violations = lint_response(bad)
    assert any(v.rule == "A" and v.field == "saturation_diagnostics" for v in violations)


def test_rule_a_passes_with_labels_present():
    good = {
        "status": "active",
        "primary_eisv": {"E": 0.4, "I": 0.6, "S": 0.1, "V": 0.0},
        "primary_eisv_source": "behavioral",
        "saturation_diagnostics": {"sat_margin": -0.2, "source": "derived"},
    }
    assert lint_response(good) == []


def test_rule_c_flags_fleet_block_without_scope():
    """§6 row 4: fleet calibration numbers inside an agent-scoped response."""
    bad = {
        "status": "active",
        "calibration_feedback": {
            "confidence": {"system_accuracy": 0.91},  # no scope label
        },
    }
    violations = lint_response(bad)
    assert any(v.rule == "C" and v.field == "calibration_feedback.confidence"
               for v in violations)


def test_rule_c_passes_with_scope_label():
    good = {
        "status": "active",
        "calibration_feedback": {
            "confidence": {"system_accuracy": 0.91, "scope": "fleet"},
        },
    }
    assert lint_response(good) == []


def test_assert_clean_raises_with_rendered_detail():
    bad = {"status": "uninitialized", "summary": "moderate | x | y basin"}
    with pytest.raises(AssertionError) as exc:
        assert_clean(bad)
    assert "provenance lint found" in str(exc.value)
    assert "summary" in str(exc.value)


def test_unbound_read_shape_is_ignorance_and_clean():
    """The §3.5 read-purity unbound payload must also lint clean."""
    from src.mcp_handlers.core import unbound_metrics_payload
    payload = unbound_metrics_payload()
    assert is_ignorance_state(payload)
    assert_clean(payload)


def test_spec_is_extensible_without_touching_engine():
    """A caller can lint a different surface by passing its own spec."""
    custom = LintSpec(labeled=(), assessment_fields=("verdict",), fleet_scoped=())
    bad = {"status": "uninitialized", "verdict": "proceed"}
    violations = lint_response(bad, custom)
    assert violations and violations[0].rule == "B"
    assert isinstance(violations[0], ProvenanceViolation)
