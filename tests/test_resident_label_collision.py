"""A resident renamed by the collision rename must stay visible.

`identity/persistence.py` renames a fresh identity to `{label}_{uuid8}` when
another ACTIVE agent already holds the label it asked for. The rename lands on
the newcomer regardless of which row is the real resident, so a ghost holding
the clean name pushes the genuine resident onto a suffixed label. Every
resident surface resolves by exact label, so the genuine resident stopped
being a candidate at all and the ghost was presented and audited in its place.

Measured 2026-08-26 over the full identity table: 201 suffixed
resident-prefixed labels, 199 of them ghosts correctly taking the suffix —
the mechanism working. Only 2 were the inverse. It is rare, and it is silent
and long-lived when it happens: `Watcher_7bf970d4` carried `persistent` under
a suffixed label from 2026-04-19 to 2026-06-14.
"""
from __future__ import annotations

import pytest

from src.http_routes.residents import (
    _resident_label_claim,
    _resident_meta_preference_key,
)


class _Meta:
    def __init__(self, label, tags=(), status="active", total_updates=0, last_update=None):
        self.label = label
        self.tags = list(tags)
        self.status = status
        self.total_updates = total_updates
        self.last_update = last_update


RESIDENT = ("persistent", "autonomous")
GHOST_UUID = "dc94fa70-6186-4862-aeb6-3fc9801263c8"
REAL_UUID = "7dea7dcb-e887-4c90-8c8a-4f3433da102b"


class TestLabelClaim:
    def test_own_uuid8_suffix_is_recognised(self):
        assert _resident_label_claim("Doctor_7dea7dcb", REAL_UUID) == "Doctor"

    def test_a_different_uuid_suffix_is_not_this_rows_rename(self):
        """⛔Only the row's OWN uuid8 counts — that is how the rename builds it."""
        assert _resident_label_claim("Doctor_7dea7dcb", GHOST_UUID) is None

    def test_deliberate_underscore_names_are_left_alone(self):
        """⛔Stripping any trailing _xxxxxxxx would merge two real agents.

        A deployment may legitimately run `Sentinel_backup` as its own agent.
        """
        assert _resident_label_claim("Sentinel_backup", REAL_UUID) is None
        assert _resident_label_claim("Sentinel", REAL_UUID) is None

    def test_degenerate_labels_do_not_claim_anything(self):
        assert _resident_label_claim("_7dea7dcb", REAL_UUID) is None
        assert _resident_label_claim(None, REAL_UUID) is None
        assert _resident_label_claim("Doctor_7dea7dcb", None) is None


class TestPreference:
    def test_tagged_resident_beats_a_ghost_holding_the_clean_name(self):
        """The case that shipped broken.

        Tags are the only ground truth for "this is the resident" — privileged,
        server-granted at mint, not self-assignable. The label is cosmetic and
        is exactly what the rename took away.
        """
        ghost = _Meta("Doctor", tags=["ephemeral"])
        real = _Meta("Doctor_7dea7dcb", tags=RESIDENT)
        assert _resident_meta_preference_key(real, exact_label=False) > \
            _resident_meta_preference_key(ghost, exact_label=True)

    def test_exact_label_still_wins_when_tags_are_equal(self):
        """The ordinary case must not regress: 199 of 201 renames are ghosts."""
        canonical = _Meta("Vigil", tags=RESIDENT)
        renamed = _Meta("Vigil_abcd1234", tags=RESIDENT)
        assert _resident_meta_preference_key(canonical, exact_label=True) > \
            _resident_meta_preference_key(renamed, exact_label=False)

    def test_active_outranks_tags(self):
        """An archived resident must never displace a live row."""
        archived_real = _Meta("Doctor_7dea7dcb", tags=RESIDENT, status="archived")
        active_ghost = _Meta("Doctor", tags=["ephemeral"])
        assert _resident_meta_preference_key(active_ghost, exact_label=True) > \
            _resident_meta_preference_key(archived_real, exact_label=False)

    def test_partial_tags_do_not_count_as_a_resident(self):
        """`persistent` alone is the Watcher_7bf970d4 shape — still a gap."""
        partial = _Meta("Watcher_7bf970d4", tags=["persistent"])
        tagged = _Meta("Watcher", tags=RESIDENT)
        assert _resident_meta_preference_key(tagged, exact_label=True) > \
            _resident_meta_preference_key(partial, exact_label=False)

    def test_updates_and_freshness_still_break_remaining_ties(self):
        busy = _Meta("Vigil", tags=RESIDENT, total_updates=40)
        idle = _Meta("Vigil", tags=RESIDENT, total_updates=0)
        assert _resident_meta_preference_key(busy) > _resident_meta_preference_key(idle)
