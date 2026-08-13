"""`audit.events.agent_id` must be joinable or NULL — never a plausible fake.

Measured 2026-08-12: of 12 `lifecycle_paused` rows in eleven days, 5 carried a
UUID and 7 carried a structured handle like `Gpt_5_20260810`. That handle
resolves in no table — `core.identities.agent_id` holds UUIDs, and the handle is
a presentation construct returned by onboard and persisted as a key nowhere. The
7 are permanently unattributable; no backfill can recover them.

The reason this is worse than plain data loss: a handle-form row is the ONLY row
that identifier ever produces. So "the paused agent went silent afterwards" is a
statement about the schema, and a pause-compliance conclusion drawn from it was
wrong. A NULL says "unattributed" and cannot be mistaken for evidence; a
plausible key that joins to nothing invites exactly that mistake.

Same rule the tool-usage recorder already states: a UUID clamp alone would only
make a forged value joinable, which is worse than NULL.
"""

from __future__ import annotations

import pytest

from src.agent_metadata_model import _joinable_audit_agent_id


UUID = "99cbe2ad-008b-429f-bf43-9e466ea5dff2"


def test_uuid_passes_through_as_the_joinable_key():
    assert _joinable_audit_agent_id(UUID) == (UUID, None)


def test_uuid_is_case_insensitive_and_trimmed():
    """Real callers pass values straight off a response envelope."""
    key, handle = _joinable_audit_agent_id(f"  {UUID.upper()}  ")
    assert key == UUID.upper()
    assert handle is None


@pytest.mark.parametrize(
    "handle",
    [
        "Gpt_5_20260810",
        "Codex_20260810",
        "Claude_20260812",
        "Codex_Coherence_Authority_Closeout_Gpt_20260812",
    ],
)
def test_structured_handles_never_become_the_key(handle):
    """These are the exact values found live in lifecycle_paused rows."""
    key, carried = _joinable_audit_agent_id(handle)
    assert key is None, f"{handle!r} would join to nothing but look like it should"
    assert carried == handle, "the handle must survive in the payload, not vanish"


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_missing_id_yields_no_key_and_no_phantom_handle(empty):
    key, carried = _joinable_audit_agent_id(empty)
    assert key is None
    assert carried is None, "whitespace must not be recorded as an agent handle"


def test_near_miss_uuids_are_rejected():
    """A clamp that accepts almost-UUIDs reintroduces the joinable-fake problem."""
    for bad in (
        UUID[:-1],                      # too short
        UUID + "0",                     # too long
        UUID.replace("-", ""),          # unhyphenated
        "zzzzzzzz-008b-429f-bf43-9e466ea5dff2",  # non-hex
    ):
        key, carried = _joinable_audit_agent_id(bad)
        assert key is None, f"{bad!r} was accepted as a joinable key"
        assert carried == bad


def test_both_write_paths_use_the_clamped_id(monkeypatch):
    """The broadcast and the direct-audit fallback must not diverge.

    They are separate code paths writing the same column; clamping one and not
    the other would leave the defect alive on whichever path a given deployment
    happens to take.
    """
    import inspect

    from src import agent_metadata_model as amm

    source = inspect.getsource(amm._emit_lifecycle_event)
    # Neither write may reference the raw parameter for the agent_id column.
    assert "agent_id=audit_agent_id" in source
    assert '"agent_id": audit_agent_id' in source
    assert "agent_id=agent_id" not in source
    assert '"agent_id": agent_id' not in source
