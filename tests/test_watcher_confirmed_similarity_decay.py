"""Regression tests for two defects Watcher flagged and review confirmed.

- compare_agents similarity divided by the signed group mean, so a negative
  mean (V is signed) reported a similarity above 1 or divided by zero.
- KG temporal decay stripped ``+00:00`` and subtracted from naive local now,
  skewing UTC-stamped ages by the host offset and raising on other offsets.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.mcp_handlers.observability.handlers import _pair_similarity
from src.storage.knowledge_graph_age_search import _discovery_age_days


class TestPairSimilarity:
    def test_negative_mean_stays_in_unit_interval(self):
        # V = [0.10, -0.09, -0.10]: mean -0.03. The signed-mean formula gave 1.345.
        values = [0.10, -0.09, -0.10]
        mean = sum(values) / len(values)
        similarity = _pair_similarity(-0.09, -0.10, mean)
        assert 0.0 <= similarity <= 1.0

    def test_mean_at_negative_epsilon_does_not_divide_by_zero(self):
        similarity = _pair_similarity(0.2, -0.202, -0.001)
        assert 0.0 <= similarity <= 1.0

    def test_identical_values_are_fully_similar(self):
        assert _pair_similarity(0.4, 0.4, 0.4) == 1.0

    def test_positive_mean_keeps_the_original_value(self):
        assert _pair_similarity(0.50, 0.52, 0.51) == pytest.approx(
            1.0 - 0.02 / 0.511
        )

    def test_large_gap_floors_at_zero(self):
        assert _pair_similarity(0.9, -0.9, 0.01) == 0.0


class TestDiscoveryAgeDays:
    NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    def test_utc_z_stamp_is_exact_regardless_of_host_offset(self):
        ts = (self.NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _discovery_age_days(ts, self.NOW) == pytest.approx(10.0)

    def test_explicit_utc_offset_matches_z(self):
        ts = (self.NOW - timedelta(days=10)).isoformat()
        assert ts.endswith("+00:00")
        assert _discovery_age_days(ts, self.NOW) == pytest.approx(10.0)

    def test_non_utc_offset_is_handled_not_raised(self):
        mst = timezone(timedelta(hours=-7))
        ts = (self.NOW - timedelta(days=3)).astimezone(mst).isoformat()
        assert ts.endswith("-07:00")
        assert _discovery_age_days(ts, self.NOW) == pytest.approx(3.0)

    def test_naive_stamp_is_read_as_local_time(self):
        local_naive = self.NOW.astimezone().replace(tzinfo=None)
        ts = (local_naive - timedelta(days=5)).isoformat()
        assert _discovery_age_days(ts, self.NOW) == pytest.approx(5.0)

    def test_future_stamp_clamps_to_zero(self):
        ts = (self.NOW + timedelta(days=1)).isoformat()
        assert _discovery_age_days(ts, self.NOW) == 0.0

    def test_unparseable_stamp_raises_value_error(self):
        # The caller catches ValueError/TypeError and skips decay for that row.
        with pytest.raises(ValueError):
            _discovery_age_days("not-a-timestamp", self.NOW)
