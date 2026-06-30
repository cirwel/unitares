"""Tests for agent lifecycle behavior.

Consolidates two areas of the lifecycle surface:

* Timezone handling in ``_agent_age_hours`` (regression guards spawned by
  the 2026-04-16 Watcher follow-up) — naive-UTC timestamps must not be
  compared against naive-local ``datetime.now()`` or orphan archival
  silently breaks in non-UTC locales.
* Tag-driven archival protection (``is_agent_protected``) and check-in
  cadence resolution (``_get_expected_interval`` / ``cadence_from_tags``)
  introduced by the Lumen-decoupling A3 changes. Back-compat fallbacks
  (``label == "Lumen"``, hardcoded label → interval map) are exercised
  explicitly so a later removal pass can see what's relied on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid
from unittest.mock import patch

import pytest

from src.agent_lifecycle import _agent_age_hours, is_agent_protected
from src.agent_metadata_model import AgentMetadata, agent_metadata
from src.agent_metadata_persistence import get_or_create_metadata
from src.background_tasks import (
    CADENCE_FROM_TAG,
    _get_expected_interval,
    cadence_from_tags,
)


# ============================================================================
# _agent_age_hours — timezone normalization
# ============================================================================

@pytest.fixture
def _now_utc():
    return datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)


def _age_meta(*, last_update: str | None, created_at: str | None = None) -> AgentMetadata:
    return AgentMetadata(
        agent_id="dummy",
        status="active",
        created_at=created_at or last_update or "",
        last_update=last_update or created_at or "",
    )


class TestAgentAgeHoursTimezoneNormalization:
    def test_naive_utc_timestamp_computes_correct_age(self, _now_utc):
        """Naive-UTC stored timestamps must be interpreted as UTC, not local."""
        six_hours_ago_utc_naive = (_now_utc - timedelta(hours=6)).replace(tzinfo=None).isoformat()
        meta = _age_meta(last_update=six_hours_ago_utc_naive)

        with patch("src.agent_lifecycle.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = _now_utc
            hours = _agent_age_hours(meta)

        assert hours is not None
        assert hours == pytest.approx(6.0, abs=0.001), (
            f"naive-UTC timestamp 6 hours ago should yield 6.0 hours, got {hours}"
        )

    def test_aware_utc_timestamp_computes_correct_age(self, _now_utc):
        """tz-aware UTC timestamp works the same as before."""
        ts = (_now_utc - timedelta(hours=2)).isoformat()
        meta = _age_meta(last_update=ts)

        with patch("src.agent_lifecycle.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = _now_utc
            hours = _agent_age_hours(meta)

        assert hours == pytest.approx(2.0, abs=0.001)

    def test_z_suffix_timestamp_computes_correct_age(self, _now_utc):
        """'Z' suffix (common ISO-8601 UTC form) normalizes correctly."""
        ts = (_now_utc - timedelta(hours=3)).replace(tzinfo=None).isoformat() + "Z"
        meta = _age_meta(last_update=ts)

        with patch("src.agent_lifecycle.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = _now_utc
            hours = _agent_age_hours(meta)

        assert hours == pytest.approx(3.0, abs=0.001)

    def test_unparseable_returns_none(self):
        meta = _age_meta(last_update="not a date")
        assert _agent_age_hours(meta) is None

    def test_empty_string_returns_none(self):
        meta = _age_meta(last_update="")
        assert _agent_age_hours(meta) is None


def test_new_metadata_timestamps_are_utc_aware():
    """Fresh metadata must use explicit UTC timestamps, not naive local time."""
    agent_id = f"test-utc-aware-{uuid.uuid4().hex[:8]}"
    try:
        meta = get_or_create_metadata(agent_id, emit_lifecycle_created=True)
        created_at = datetime.fromisoformat(meta.created_at)
        last_update = datetime.fromisoformat(meta.last_update)
        created_event = datetime.fromisoformat(meta.lifecycle_events[0]["timestamp"])

        assert created_at.tzinfo is not None
        assert last_update.tzinfo is not None
        assert created_event.tzinfo is not None
        assert created_at.utcoffset() == timedelta(0)
        assert last_update.utcoffset() == timedelta(0)
        assert created_event.utcoffset() == timedelta(0)
    finally:
        agent_metadata.pop(agent_id, None)


def test_metadata_cache_hydration_does_not_emit_created_event():
    """Cache-only metadata creation must not look like a real lifecycle create."""
    agent_id = f"test-cache-hydrate-{uuid.uuid4().hex[:8]}"
    try:
        meta = get_or_create_metadata(agent_id)

        assert meta.created_at is not None
        assert meta.last_update is not None
        assert meta.lifecycle_events == []
    finally:
        agent_metadata.pop(agent_id, None)


# ============================================================================
# is_agent_protected — tag-driven
# ============================================================================

def _protection_meta(label=None, tags=None, trust_tier=None):
    return SimpleNamespace(
        label=label,
        display_name=label,
        tags=tags or [],
        trust_tier=trust_tier,
    )


class TestAgentProtection:
    def test_persistent_tag_protects_agent_without_name_match(self):
        meta = _protection_meta(label="SomeOtherAgent", tags=["persistent"])
        assert is_agent_protected("some-uuid", meta) is True

    def test_protected_tag_protects_agent_without_name_match(self):
        meta = _protection_meta(label="SomeOtherAgent", tags=["protected"])
        assert is_agent_protected("some-uuid", meta) is True

    def test_untagged_ephemeral_agent_is_not_protected(self):
        meta = _protection_meta(label="claude_cirwel_20260412", tags=[])
        assert is_agent_protected("some-uuid", meta) is False

    def test_pioneer_tag_still_protects(self):
        """Preserve the pre-existing ``pioneer`` tag behaviour."""
        meta = _protection_meta(label="Pioneer", tags=["pioneer"])
        assert is_agent_protected("some-uuid", meta) is True

    def test_verified_trust_tier_still_protects(self):
        """Trust-tier gating is independent of tag-based protection."""
        meta = _protection_meta(label="TrustedBot", tags=[], trust_tier="verified")
        assert is_agent_protected("some-uuid", meta) is True

    def test_lumen_label_backcompat_still_protects(self):
        """Back-compat: Lumen is protected by label until she's tagged ``persistent``.

        When Lumen carries a ``persistent`` tag, the label path is dead code and
        can be deleted. Until then, this guard keeps her safe from archival.
        """
        meta = _protection_meta(label="Lumen", tags=[])
        assert is_agent_protected("lumen-uuid", meta) is True


# ============================================================================
# cadence_from_tags — pure helper
# ============================================================================

class TestCadenceFromTags:
    def test_returns_interval_for_known_tag(self):
        assert cadence_from_tags(["cadence.5min"]) == 300
        assert cadence_from_tags(["cadence.30min"]) == 1800
        assert cadence_from_tags(["cadence.1hr"]) == 3600
        assert cadence_from_tags(["cadence.24hr"]) == 86400

    def test_picks_first_match(self):
        # Multiple cadence tags is a user error, but first-wins is a stable rule.
        assert cadence_from_tags(["cadence.5min", "cadence.30min"]) == 300

    def test_ignores_non_cadence_tags(self):
        assert cadence_from_tags(["persistent", "embodied", "autonomous"]) is None

    def test_handles_none_and_empty(self):
        assert cadence_from_tags(None) is None
        assert cadence_from_tags([]) is None

    def test_constant_keys_are_well_formed(self):
        """Every declared cadence key must be ``cadence.<suffix>`` with a positive int value."""
        for tag, seconds in CADENCE_FROM_TAG.items():
            assert tag.startswith("cadence."), tag
            assert isinstance(seconds, int) and seconds > 0, (tag, seconds)


# ============================================================================
# _get_expected_interval — event-driven > tag > label fallback > embodied/autonomous default
# ============================================================================

class TestExpectedInterval:
    @pytest.fixture(autouse=True)
    def _inject_label_intervals(self, monkeypatch):
        """Label-based intervals are deployment-local now (UNITARES_CLASS_CALIBRATION
        overlay); inject representative test values so the label-fallback path has
        a map to exercise, without depending on shipped per-resident config."""
        import src.background_tasks as bt
        monkeypatch.setattr(bt, "_PERSISTENT_AGENT_INTERVALS",
                            {"Vigil": 1800, "Lumen": 300, "Sentinel": 600, "Watcher": 21600})

    def test_prefers_cadence_tag_over_label(self):
        """Tag-driven cadence wins even when the label matches the legacy map."""
        meta = _protection_meta(label="Lumen", tags=["cadence.10min"])
        assert _get_expected_interval(meta) == 600

    def test_falls_back_to_label_map_when_untagged(self):
        """Back-compat: Lumen label still resolves to 300s until tagged."""
        meta = _protection_meta(label="Lumen", tags=[])
        assert _get_expected_interval(meta) == 300

    def test_watcher_event_driven_registry_suppresses_cadence(self):
        """Watcher is hook-driven; cadence fallback must not page between edits."""
        meta = _protection_meta(label="Watcher", tags=["persistent", "autonomous"])
        assert _get_expected_interval(meta) is None

    def test_falls_back_to_embodied_default(self):
        meta = _protection_meta(label="SomeEmbodied", tags=["embodied"])
        assert _get_expected_interval(meta) == 300

    def test_falls_back_to_autonomous_default(self):
        meta = _protection_meta(label="SomeAutonomous", tags=["autonomous"])
        assert _get_expected_interval(meta) == 300

    def test_returns_none_for_ephemeral_untagged_agent(self):
        meta = _protection_meta(label="claude_cirwel_20260412", tags=[])
        assert _get_expected_interval(meta) is None

    def test_vigil_cadence_30min_tag_matches_legacy_default(self):
        """Tag-based resolution for Vigil equals the old hardcoded value.

        Guards against accidental drift in cadence semantics during the migration:
        whatever Vigil's silence threshold was before, she keeps after tagging.
        """
        tagged = _protection_meta(label="Vigil", tags=["cadence.30min"])
        untagged = _protection_meta(label="Vigil", tags=[])
        assert _get_expected_interval(tagged) == _get_expected_interval(untagged) == 1800

    def test_chronicler_daily_cadence_tag_is_respected(self):
        """Chronicler is daily; unknown cadence tags must not fall to 5 minutes."""
        meta = _protection_meta(
            label="Chronicler",
            tags=["persistent", "autonomous", "cadence.24hr"],
        )
        assert _get_expected_interval(meta) == 86400
