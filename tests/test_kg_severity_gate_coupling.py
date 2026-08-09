"""The KG severity gates must guard `update` as tightly as they guard `store`.

`store()` refuses to create a high/critical discovery without a registered,
ownership-verified caller (`_resolve_store_writer` -> `require_registered_agent`,
then `_authorize_store_discovery` -> `verify_agent_ownership`).

Both update-side gates used to key on the *stored* severity, so raising a row
low -> critical was judged against `low`: the anonymous low-friction path was
taken and the ownership check returned early, landing a critical row that
`store()` would have refused. Escalation also buys write-protection, since
`_requested_non_owner_edits` then blocks non-owners from editing the row it
just created.

That is the same mint-vs-gate coupling class as #598 (mint produced `mcp_*`
names the reserved-prefix validator rejected) and #1056 (the named-caller
variant). The invariant these tests pin is the general form:

    a severity a caller cannot store() is a severity it cannot update() into.

KG: 2026-08-09T04:37:36.796821+00:00
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.knowledge.handlers import (  # noqa: E402
    _GATED_SEVERITIES,
    _KnowledgeUpdateRequest,
    _authorize_high_severity_update,
    _effective_update_severity,
    _resolve_update_writer,
    _UpdateResponseError,
)

ALL_SEVERITIES = ("low", "medium", "high", "critical")


def _request(severity=None, **overrides):
    fields = {
        "arguments": {"discovery_id": "d-1"},
        "discovery_id": "d-1",
        "status": None,
        "details": None,
        "resolution_note": None,
        "summary": None,
        "severity": severity,
        "discovery_type": None,
        "tags": None,
        "superseded_by": None,
    }
    fields.update(overrides)
    return _KnowledgeUpdateRequest(**fields)


def _discovery(severity, agent_id="owner-uuid"):
    return SimpleNamespace(severity=severity, agent_id=agent_id, id="d-1")


class TestEffectiveSeverity:
    """The higher of stored and requested is what the gates must answer to."""

    @pytest.mark.parametrize(
        "stored,requested,expected",
        [
            # Escalation — the ungated hole. Effective must be the REQUESTED value.
            ("low", "critical", "critical"),
            ("low", "high", "high"),
            ("medium", "high", "high"),
            # De-escalation — effective stays the STORED value, so downgrading
            # someone else's critical finding remains gated. Hiding a real
            # problem is at least as bad as inventing a fake one.
            ("critical", "low", "critical"),
            ("high", "medium", "high"),
            # No severity edit — stored value carries.
            ("critical", None, "critical"),
            ("low", None, "low"),
            # Within-band moves stay ungated.
            ("low", "medium", "medium"),
        ],
    )
    def test_takes_the_higher_rank(self, stored, requested, expected):
        effective = _effective_update_severity(
            _request(severity=requested), _discovery(stored)
        )
        assert effective == expected

    def test_unparseable_requested_severity_does_not_waive_the_gate(self):
        """A junk severity must not read as 'not high', bypassing the check.

        The value is rejected later with a proper enum error; until then the
        gate falls back to the stored severity rather than letting an
        unparseable string open the door.
        """
        effective = _effective_update_severity(
            _request(severity="NOT_A_SEVERITY"), _discovery("critical")
        )
        assert effective == "critical"

    def test_missing_stored_severity_uses_requested(self):
        effective = _effective_update_severity(
            _request(severity="critical"), _discovery(None)
        )
        assert effective == "critical"

    def test_case_is_normalized(self):
        effective = _effective_update_severity(
            _request(severity="CRITICAL"), _discovery("low")
        )
        assert effective == "critical"


class TestWriterGateCoupling:
    """Escalating into the gated band must demand a registered agent."""

    def test_escalation_requires_registered_agent(self):
        """The regression: low -> critical used to take the anonymous path."""
        with patch(
            "src.mcp_handlers.knowledge.handlers.require_registered_agent",
            return_value=("caller", None),
        ) as registered, patch(
            "src.mcp_handlers.knowledge.handlers._resolve_low_friction_writer",
            return_value=("anon", None, True),
        ) as low_friction:
            agent_id = _resolve_update_writer(
                _request(severity="critical"), _discovery("low")
            )

        registered.assert_called_once()
        low_friction.assert_not_called()
        assert agent_id == "caller"

    def test_non_escalating_update_keeps_the_low_friction_path(self):
        """Ungated edits must stay cheap — this is not a blanket tightening."""
        with patch(
            "src.mcp_handlers.knowledge.handlers.require_registered_agent",
            return_value=("caller", None),
        ) as registered, patch(
            "src.mcp_handlers.knowledge.handlers._resolve_low_friction_writer",
            return_value=("anon", None, True),
        ) as low_friction:
            agent_id = _resolve_update_writer(
                _request(severity="medium"), _discovery("low")
            )

        low_friction.assert_called_once()
        registered.assert_not_called()
        assert agent_id == "anon"

    def test_stored_high_severity_still_gated(self):
        """Pre-existing behaviour, pinned so the refactor cannot loosen it."""
        with patch(
            "src.mcp_handlers.knowledge.handlers.require_registered_agent",
            return_value=("caller", None),
        ) as registered, patch(
            "src.mcp_handlers.knowledge.handlers._resolve_low_friction_writer",
            return_value=("anon", None, True),
        ):
            _resolve_update_writer(_request(status="resolved"), _discovery("critical"))

        registered.assert_called_once()


class TestOwnershipGateCoupling:
    """Escalating into the gated band must demand verified ownership."""

    def test_escalation_runs_the_ownership_check(self):
        """The second half of the hole: the check used to return early."""
        with patch(
            "src.mcp_handlers.utils.verify_agent_ownership", return_value=False
        ) as verify:
            with pytest.raises(_UpdateResponseError):
                _authorize_high_severity_update(
                    _request(severity="critical"), _discovery("low"), "some-caller"
                )

        verify.assert_called_once()

    def test_escalating_owner_is_allowed_through(self):
        """Correcting the severity of your own finding stays possible."""
        with patch(
            "src.mcp_handlers.utils.verify_agent_ownership", return_value=True
        ):
            _authorize_high_severity_update(
                _request(severity="critical"),
                _discovery("low", agent_id="owner-uuid"),
                "owner-uuid",
            )

    def test_escalating_non_owner_is_refused_even_when_authenticated(self):
        """Ownership-verified but not the owner: severity is an owner-only edit."""
        with patch(
            "src.mcp_handlers.utils.verify_agent_ownership", return_value=True
        ):
            with pytest.raises(_UpdateResponseError):
                _authorize_high_severity_update(
                    _request(severity="critical"),
                    _discovery("low", agent_id="owner-uuid"),
                    "a-different-caller",
                )

    def test_ungated_update_skips_the_ownership_check(self):
        with patch(
            "src.mcp_handlers.utils.verify_agent_ownership", return_value=False
        ) as verify:
            _authorize_high_severity_update(
                _request(severity="medium"), _discovery("low"), "anon"
            )

        verify.assert_not_called()


class TestStoreUpdateCouplingInvariant:
    """The general rule, stated once: store and update must agree.

    Parametrized over every severity so a future band change (a new level, or
    moving `medium` into the gated set) fails here rather than silently
    reopening the escalation path on one side only.
    """

    @pytest.mark.parametrize("target", ALL_SEVERITIES)
    def test_a_severity_you_cannot_store_is_one_you_cannot_update_into(self, target):
        store_gate_applies = target in _GATED_SEVERITIES

        update_gate_applies = (
            _effective_update_severity(_request(severity=target), _discovery("low"))
            in _GATED_SEVERITIES
        )

        assert update_gate_applies == store_gate_applies, (
            f"store() and update() disagree for severity={target!r}: "
            f"store gated={store_gate_applies}, update gated={update_gate_applies}. "
            "A gate that guards only one write path is not a gate."
        )
