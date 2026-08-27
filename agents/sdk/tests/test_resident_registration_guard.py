"""The SDK must refuse to run an unregistered `persistent` agent.

`persistent`/`autonomous` are privileged: granted only at mint, only to names
on the server's UNITARES_RESIDENTS roster, and never self-assignable. So
`_reconcile_resident_tags` CANNOT repair an off-roster resident — it retries
and warns every cycle, forever, without naming the roster, while the orphan
sweep archives the identity.

Bootstrap is the last moment the operator can fix it cheaply. Refuse there.
"""
from __future__ import annotations

import pytest

from unitares_sdk.agent import GovernanceAgent
from unitares_sdk.errors import ResidentRegistrationRefused


class _Client:
    def __init__(self, registration):
        self.last_resident_registration = registration


def _agent(persistent: bool) -> GovernanceAgent:
    a = GovernanceAgent.__new__(GovernanceAgent)
    a.name = "MyResident"
    a.persistent = persistent
    return a


def test_refuses_when_the_name_was_not_on_the_roster():
    client = _Client({
        "status": "not_on_roster",
        "detail": "add 'MyResident' to UNITARES_RESIDENTS ...",
    })
    with pytest.raises(ResidentRegistrationRefused) as exc:
        _agent(persistent=True)._verify_resident_registration(client)
    assert "UNITARES_RESIDENTS" in str(exc.value)


def test_refuses_on_a_residentless_install_too():
    """persistent=True on a deployment with no roster is still a resident that
    is not protected — the operator asked for something they did not get."""
    client = _Client({"status": "no_roster_configured", "detail": "..."})
    with pytest.raises(ResidentRegistrationRefused):
        _agent(persistent=True)._verify_resident_registration(client)


def test_registered_passes():
    client = _Client({"status": "registered", "granted_tags": ["persistent", "autonomous"]})
    _agent(persistent=True)._verify_resident_registration(client)


def test_non_persistent_agents_are_unaffected():
    """An ordinary agent needs no roster entry and must not be blocked."""
    client = _Client({"status": "not_on_roster", "detail": "..."})
    _agent(persistent=False)._verify_resident_registration(client)


def test_older_server_without_the_field_is_not_treated_as_a_failure():
    """⛔Absence of the field is an old server, NOT evidence of a failed
    registration. Raising here would break every existing resident against a
    server that has not been upgraded yet."""
    _agent(persistent=True)._verify_resident_registration(_Client(None))
    _agent(persistent=True)._verify_resident_registration(_Client("unexpected"))
