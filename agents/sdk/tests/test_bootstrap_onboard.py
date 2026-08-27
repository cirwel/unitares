"""The published SDK could not onboard against a live server at all.

Reproduced 2026-08-26 with unitares-sdk 0.2.2 installed from PyPI into a clean
venv, following only the README's own "30-line resident":

    1 validation error for OnboardResult
    client_session_id  Field required [type=missing]

Two defects behind it:

  * GovernanceAgent's fresh-onboard path called `onboard(name)` with no
    `force_new`. A bare onboard is ambiguous to the server -- it cannot tell a
    first run from a resident that lost its anchor -- so it answers
    `status: lineage_declaration_required`, correctly.
  * That guidance reply carries no uuid and no client_session_id, so
    OnboardResult refused to validate and the agent died on a schema complaint
    about a field the author had never heard of. The server's `hint`,
    `next_step` and three `safe_options` were all discarded.

The server's response was the good part here. The SDK threw it away.
"""
from __future__ import annotations

import pytest

from unitares_sdk.client import _raise_if_guidance
from unitares_sdk.errors import IdentityGuidanceReturned

GUIDANCE = {
    "success": True,
    "status": "lineage_declaration_required",
    "hint": "Bare onboard() is ambiguous — pass parent_agent_id=<prior UUID> "
            "to continue prior work, OR force_new=true to confirm a fresh "
            "process-instance with no lineage.",
    "next_step": "Call onboard(force_new=true) to mint a fresh process identity",
    "safe_options": [{"action": "start_fresh", "call": "onboard(force_new=true)"}],
}


def test_guidance_raises_instead_of_failing_validation():
    with pytest.raises(IdentityGuidanceReturned) as exc:
        _raise_if_guidance("onboard", GUIDANCE)
    assert exc.value.status == "lineage_declaration_required"


def test_the_error_carries_the_servers_own_words():
    """⛔The whole point. A pydantic 'field required' told the author nothing;
    the server had already written the answer."""
    with pytest.raises(IdentityGuidanceReturned) as exc:
        _raise_if_guidance("onboard", GUIDANCE)
    msg = str(exc.value)
    assert "force_new=true" in msg
    assert exc.value.next_step
    assert exc.value.safe_options[0]["call"] == "onboard(force_new=true)"


def test_a_real_identity_response_passes_through():
    """Must not intercept a successful mint."""
    _raise_if_guidance("onboard", {
        "success": True, "agent_uuid": "u", "client_session_id": "s",
        "status": "something_informational",
    })


def test_an_identity_without_a_status_passes_through():
    _raise_if_guidance("onboard", {"success": True, "client_session_id": "s"})


def test_non_dict_payloads_are_ignored():
    _raise_if_guidance("onboard", None)
    _raise_if_guidance("onboard", "text")


def test_status_alone_without_an_identity_is_guidance():
    """A status with no uuid AND no session is the shape that broke."""
    with pytest.raises(IdentityGuidanceReturned):
        _raise_if_guidance("onboard", {"success": True, "status": "identity_required"})


class TestFreshBootstrapDeclaresItself:
    def test_agent_passes_force_new_on_fresh_onboard(self):
        """Reaching the fresh path IS the deliberate bootstrap — the anchor is
        absent and, for refuse_fresh_onboard residents, UNITARES_FIRST_RUN=1
        was set. Declaring it is what the server asks for."""
        import inspect
        from unitares_sdk import agent as agent_mod
        src = inspect.getsource(agent_mod.GovernanceAgent._ensure_identity)
        assert "force_new=True" in src
