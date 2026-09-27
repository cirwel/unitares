"""Tests for the calibration primitives — Jeffreys interval, file
classifier, exponential-decay weighting, and per-(pattern, file_class)
precision aggregation."""

from datetime import datetime, timedelta, timezone

import pytest

from agents.watcher.calibration import jeffreys_lower_bound


class TestJeffreysLowerBound:
    """Beta(0.5, 0.5) prior. Posterior is Beta(0.5+s, 0.5+f). We return
    the 2.5% lower quantile, which behaves well at small N (no division
    by zero, monotonic in N) but is NOT 0.5 at N=0 — it's near zero. A
    min_n gate at the demotion callsite handles 'unmeasured' separately
    from 'measured-as-zero'."""

    def test_returns_float_in_unit_interval(self):
        for s, f in [(0, 0), (1, 0), (0, 1), (5, 5), (50, 50), (100, 0), (0, 100)]:
            lb = jeffreys_lower_bound(s, f)
            assert 0.0 <= lb <= 1.0, f"({s},{f}) → {lb} out of [0,1]"

    def test_n_zero_returns_low_value(self):
        # No observations — posterior is Beta(0.5, 0.5), 2.5% quantile ≈ 0.0015
        lb = jeffreys_lower_bound(0, 0)
        assert lb < 0.01, f"N=0 should return near-zero, got {lb}"

    def test_all_successes_high_n_high_lower_bound(self):
        # 100 successes, 0 failures: posterior tightly above 0.95
        lb = jeffreys_lower_bound(100, 0)
        assert lb > 0.95, f"100/0 should give high lower bound, got {lb}"

    def test_all_failures_high_n_zero_lower_bound(self):
        lb = jeffreys_lower_bound(0, 100)
        assert lb < 0.05, f"0/100 should give near-zero lower bound, got {lb}"

    def test_monotonic_in_successes(self):
        # Adding successes can only raise the lower bound
        lo = jeffreys_lower_bound(5, 5)
        hi = jeffreys_lower_bound(15, 5)
        assert hi > lo, f"more successes should raise lower bound: {lo} → {hi}"

    def test_negative_input_raises(self):
        with pytest.raises(ValueError):
            jeffreys_lower_bound(-1, 0)
        with pytest.raises(ValueError):
            jeffreys_lower_bound(0, -1)

    def test_fractional_input_accepted(self):
        # Decay-weighted counts are floats, not ints. The function must accept them.
        lb = jeffreys_lower_bound(2.5, 7.5)
        assert 0.0 <= lb <= 1.0


from agents.watcher.calibration import classify_file, FileClass


class TestClassifyFile:
    """File class is the second axis of the calibration bucket. Six
    coarse classes are enough to capture the heterogeneity the dialectic
    flagged (a regex that's 90% precise on app code and 10% on tests
    averages to ~50% globally and gets demoted everywhere)."""

    def test_test_files(self):
        assert classify_file("/repo/agents/watcher/tests/test_x.py") == FileClass.TEST
        assert classify_file("/repo/tests/integration/foo.py") == FileClass.TEST
        assert classify_file("/repo/foo_test.py") == FileClass.TEST
        assert classify_file("/repo/test_bar.py") == FileClass.TEST

    def test_migration_files(self):
        assert classify_file("/repo/migrations/018_foo.sql") == FileClass.MIGRATION
        assert classify_file("/repo/db/migrations/up.py") == FileClass.MIGRATION

    def test_generated_files(self):
        assert classify_file("/repo/build/foo.py") == FileClass.GENERATED
        assert classify_file("/repo/dist/bundle.js") == FileClass.GENERATED
        assert classify_file("/repo/__pycache__/x.pyc") == FileClass.GENERATED
        assert classify_file("/repo/foo.pb.go") == FileClass.GENERATED

    def test_config_files(self):
        assert classify_file("/repo/pyproject.toml") == FileClass.CONFIG
        assert classify_file("/repo/setup.cfg") == FileClass.CONFIG
        assert classify_file("/repo/.github/workflows/ci.yml") == FileClass.CONFIG
        assert classify_file("/repo/Makefile") == FileClass.CONFIG

    def test_doc_files(self):
        assert classify_file("/repo/README.md") == FileClass.DOC
        assert classify_file("/repo/docs/foo.md") == FileClass.DOC
        assert classify_file("/repo/CHANGELOG") == FileClass.DOC

    def test_app_default(self):
        assert classify_file("/repo/src/server.py") == FileClass.APP
        assert classify_file("/repo/agents/watcher/agent.py") == FileClass.APP
        assert classify_file("/repo/governance_core/eisv.py") == FileClass.APP

    def test_test_wins_over_app(self):
        # tests/ is the strongest signal — it sits inside src/ in some layouts
        assert classify_file("/repo/src/tests/foo.py") == FileClass.TEST


from agents.watcher.calibration import decay_weight, parse_iso_z


class TestDecayWeight:
    """Exponential decay replaces the 90-day hard cutoff. Half-life ~30d
    means a 60-day-old observation contributes 0.25× of a fresh one.
    Old observations don't fall off a cliff; they just matter less."""

    def test_zero_age_full_weight(self):
        now = datetime(2026, 4, 27, tzinfo=timezone.utc)
        assert decay_weight(now, now, half_life_days=30.0) == pytest.approx(1.0)

    def test_one_half_life_half_weight(self):
        now = datetime(2026, 4, 27, tzinfo=timezone.utc)
        thirty_days_ago = now - timedelta(days=30)
        assert decay_weight(thirty_days_ago, now, half_life_days=30.0) == pytest.approx(0.5, rel=1e-9)

    def test_two_half_lives_quarter_weight(self):
        now = datetime(2026, 4, 27, tzinfo=timezone.utc)
        sixty_days_ago = now - timedelta(days=60)
        assert decay_weight(sixty_days_ago, now, half_life_days=30.0) == pytest.approx(0.25, rel=1e-9)

    def test_future_timestamps_clamp_to_one(self):
        """Clock skew shouldn't manufacture > 1.0 weights."""
        now = datetime(2026, 4, 27, tzinfo=timezone.utc)
        future = now + timedelta(hours=1)
        assert decay_weight(future, now, half_life_days=30.0) == pytest.approx(1.0)

    def test_naive_datetime_raises(self):
        now = datetime.now(timezone.utc)
        naive = datetime(2026, 4, 1)
        with pytest.raises(ValueError):
            decay_weight(naive, now, half_life_days=30.0)


class TestParseIsoZ:
    """Tolerate the two timestamp formats actually present in
    findings.jsonl: '2026-04-20T12:34:56Z' (Watcher's own writes) and
    '2026-04-20T12:34:56+00:00' (governance writes via Python isoformat)."""

    def test_z_suffix(self):
        ts = parse_iso_z("2026-04-20T12:34:56Z")
        assert ts == datetime(2026, 4, 20, 12, 34, 56, tzinfo=timezone.utc)

    def test_plus_zero_suffix(self):
        ts = parse_iso_z("2026-04-20T12:34:56+00:00")
        assert ts == datetime(2026, 4, 20, 12, 34, 56, tzinfo=timezone.utc)

    def test_garbage_returns_none(self):
        assert parse_iso_z("not a date") is None
        assert parse_iso_z("") is None
        assert parse_iso_z(None) is None  # type: ignore[arg-type]


from agents.watcher.calibration import (
    PATTERN_RULE_EPOCHS,
    PRECISION_REASONS_TRUE_NEGATIVE,
    BucketStats,
    precision_by_pattern_and_class,
)


def _row(*, pattern, file, status, ts, reason=None, confirmed_at=None, dismissed_at=None):
    """Helper: build a findings.jsonl-shaped dict."""
    r = {
        "pattern": pattern,
        "file": file,
        "line": 1,
        "hint": "h",
        "severity": "medium",
        "status": status,
        "detected_at": ts,
        "fingerprint": "abcd1234",
        "violation_class": "BEH",
    }
    if confirmed_at:
        r["confirmed_at"] = confirmed_at
    if dismissed_at:
        r["dismissed_at"] = dismissed_at
    if reason is not None:
        r["resolution_reason"] = reason
    return r


class TestPrecisionByPatternAndClass:
    """The aggregator combines decay-weighting and the reason filter to
    produce per-bucket {weighted_confirmed, weighted_dismissed, ci_lower}.

    Critical behaviors:
      - Only confirmed/dismissed rows count (open/surfaced/aged_out skipped)
      - 'wont_fix', 'out_of_scope', 'unclear' dismissals are EXCLUDED from
        precision math (precision means 'TP / (TP + FP)', and these aren't
        false positives)
      - Legacy free-text reasons are excluded too (no taxonomy alignment)
      - Buckets with weighted_n < min_weighted_n return ci_lower=None
        (the demotion callsite gates on this)
    """

    NOW = datetime(2026, 4, 27, tzinfo=timezone.utc)
    YESTERDAY = "2026-04-26T12:00:00Z"

    def test_empty_findings_empty_dict(self):
        result = precision_by_pattern_and_class([], now=self.NOW)
        assert result == {}

    def test_only_confirmed_dismissed_count(self):
        rows = [
            _row(pattern="P1", file="/a/src/x.py", status="open", ts=self.YESTERDAY),
            _row(pattern="P1", file="/a/src/x.py", status="surfaced", ts=self.YESTERDAY),
            _row(pattern="P1", file="/a/src/x.py", status="aged_out", ts=self.YESTERDAY),
            _row(pattern="P1", file="/a/src/x.py", status="confirmed", ts=self.YESTERDAY),
            _row(pattern="P1", file="/a/src/x.py", status="dismissed", reason="fp", ts=self.YESTERDAY),
        ]
        result = precision_by_pattern_and_class(rows, now=self.NOW, min_weighted_n=0.5)
        bucket = result[("P1", "app")]
        assert bucket.weighted_confirmed == pytest.approx(1.0, rel=0.05)
        assert bucket.weighted_dismissed == pytest.approx(1.0, rel=0.05)

    def test_wont_fix_excluded_from_dismissed(self):
        rows = [
            _row(pattern="P1", file="/a/src/x.py", status="confirmed", ts=self.YESTERDAY),
            _row(pattern="P1", file="/a/src/x.py", status="dismissed", reason="wont_fix", ts=self.YESTERDAY),
            _row(pattern="P1", file="/a/src/x.py", status="dismissed", reason="dup", ts=self.YESTERDAY),
            _row(pattern="P1", file="/a/src/x.py", status="dismissed", reason="unclear", ts=self.YESTERDAY),
        ]
        result = precision_by_pattern_and_class(rows, now=self.NOW, min_weighted_n=0.5)
        bucket = result[("P1", "app")]
        assert bucket.weighted_confirmed == pytest.approx(1.0, rel=0.05)
        assert bucket.weighted_dismissed == pytest.approx(0.0, abs=0.01)

    def test_legacy_free_text_reason_excluded(self):
        rows = [
            _row(pattern="P1", file="/a/src/x.py", status="dismissed",
                 reason="this was a false alarm imo", ts=self.YESTERDAY),
            _row(pattern="P1", file="/a/src/x.py", status="dismissed",
                 reason="fp", ts=self.YESTERDAY),
        ]
        result = precision_by_pattern_and_class(rows, now=self.NOW, min_weighted_n=0.5)
        bucket = result[("P1", "app")]
        assert bucket.weighted_dismissed == pytest.approx(1.0, rel=0.05)

    def test_buckets_split_on_file_class(self):
        rows = [
            _row(pattern="P1", file="/a/src/foo.py", status="confirmed", ts=self.YESTERDAY),
            _row(pattern="P1", file="/a/tests/test_foo.py", status="dismissed",
                 reason="fp", ts=self.YESTERDAY),
        ]
        result = precision_by_pattern_and_class(rows, now=self.NOW, min_weighted_n=0.5)
        assert ("P1", "app") in result
        assert ("P1", "test") in result
        assert result[("P1", "app")].weighted_confirmed > 0
        assert result[("P1", "app")].weighted_dismissed == 0
        assert result[("P1", "test")].weighted_confirmed == 0
        assert result[("P1", "test")].weighted_dismissed > 0

    def test_decay_applied(self):
        """A 60-day-old confirmation contributes 0.25× of a fresh one."""
        rows = [
            _row(pattern="P1", file="/a/src/x.py", status="confirmed",
                 ts=(self.NOW - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")),
            _row(pattern="P1", file="/a/src/x.py", status="confirmed",
                 ts=self.NOW.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ]
        result = precision_by_pattern_and_class(rows, now=self.NOW, half_life_days=30.0,
                                                min_weighted_n=0.5)
        bucket = result[("P1", "app")]
        assert bucket.weighted_confirmed == pytest.approx(1.25, rel=0.01)

    def test_min_weighted_n_returns_none_ci(self):
        rows = [
            _row(pattern="P1", file="/a/src/x.py", status="confirmed", ts=self.YESTERDAY),
        ]
        result = precision_by_pattern_and_class(rows, now=self.NOW, min_weighted_n=10.0)
        bucket = result[("P1", "app")]
        assert bucket.ci_lower is None, "below min_weighted_n should yield ci_lower=None"

    def test_above_min_weighted_n_returns_ci(self):
        rows = [
            _row(pattern="P1", file="/a/src/x.py", status="confirmed", ts=self.YESTERDAY)
            for _ in range(20)
        ]
        result = precision_by_pattern_and_class(rows, now=self.NOW, min_weighted_n=10.0)
        bucket = result[("P1", "app")]
        assert bucket.ci_lower is not None
        assert bucket.ci_lower > 0.7

    def test_unparseable_timestamp_skipped(self):
        rows = [
            _row(pattern="P1", file="/a/src/x.py", status="confirmed", ts="garbage"),
            _row(pattern="P1", file="/a/src/x.py", status="confirmed", ts=self.YESTERDAY),
        ]
        result = precision_by_pattern_and_class(rows, now=self.NOW, min_weighted_n=0.5)
        bucket = result[("P1", "app")]
        assert bucket.weighted_confirmed == pytest.approx(1.0, rel=0.05)


class TestRuleEpochs:
    """A resolution grades the rule that produced the finding. Rows of a
    pattern detected before its rule last changed measure a rule Watcher no
    longer runs, so they stay out of that pattern's precision."""

    NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
    EPOCHS = {"P1": "2026-09-26T00:00:00Z"}

    def _fp(self, detected_at, file="/a/src/x.py", **kw):
        return _row(pattern="P1", file=file, status="dismissed", reason="fp",
                    ts=detected_at, dismissed_at="2026-09-26T12:00:00Z", **kw)

    def test_rows_detected_before_the_epoch_are_left_out(self):
        rows = [self._fp("2026-09-25T23:59:59Z"), self._fp("2026-09-26T00:00:00Z")]
        result = precision_by_pattern_and_class(
            rows, now=self.NOW, min_weighted_n=0.5, rule_epochs=self.EPOCHS)
        assert result[("P1", "app")].weighted_dismissed == pytest.approx(0.99, rel=0.02)

    def test_epoch_reads_detected_at_not_the_resolution_time(self):
        # Dismissed after the epoch, detected before it: still the old rule.
        rows = [self._fp("2026-09-20T00:00:00Z")]
        result = precision_by_pattern_and_class(
            rows, now=self.NOW, min_weighted_n=0.5, rule_epochs=self.EPOCHS)
        assert ("P1", "app") not in result

    def test_row_without_detected_at_is_left_out_for_a_listed_pattern(self):
        row = self._fp("2026-09-26T06:00:00Z")
        del row["detected_at"]
        result = precision_by_pattern_and_class(
            [row], now=self.NOW, min_weighted_n=0.5, rule_epochs=self.EPOCHS)
        assert result == {}

    def test_unlisted_pattern_keeps_its_history(self):
        rows = [_row(pattern="P2", file="/a/src/x.py", status="dismissed", reason="fp",
                     ts="2026-01-01T00:00:00Z", dismissed_at="2026-09-26T12:00:00Z")]
        result = precision_by_pattern_and_class(
            rows, now=self.NOW, min_weighted_n=0.5, rule_epochs=self.EPOCHS)
        assert result[("P2", "app")].weighted_dismissed > 0

    def test_p006_triage_before_its_rule_change_no_longer_demotes(self):
        """The 2026-09-23/24 triage dismissed 25 P006 app findings of the
        earlier rule. Under the default table they leave no measured bucket,
        so the demotion callsite (which needs a ci_lower) cannot fire."""
        rows = [
            _row(pattern="P006", file=f"/wt/tree-{i}/src/mod_{i}.py", status="dismissed",
                 reason="fp", ts="2026-09-23T20:00:00Z",
                 dismissed_at="2026-09-24T20:00:00Z")
            for i in range(25)
        ]
        result = precision_by_pattern_and_class(rows, now=self.NOW)
        assert ("P006", "app") not in result


def test_precision_reasons_constant_shape():
    """Document the canonical taxonomy. Precision math counts as TN ONLY
    the reasons that mean 'this finding was a false positive'."""
    assert PRECISION_REASONS_TRUE_NEGATIVE == frozenset({"fp"})


def test_pattern_rule_epochs_parse():
    """Every entry must parse: an unparseable one is silently ignored."""
    for pattern, raw in PATTERN_RULE_EPOCHS.items():
        assert parse_iso_z(raw) is not None, pattern
    assert "P006" in PATTERN_RULE_EPOCHS


from agents.watcher.calibration import probe_rate_for_n, should_probe


class TestProbeRateForN:
    """Adaptive probe rate. Smaller buckets need higher exploration so
    they can acquire signal; larger buckets settle into exploit mode.

    Schedule:
      - n < 10: probe rate = 1.0 (no demotion at all — handled at callsite)
      - 10 ≤ n < 30: probe rate = 1/3
      - 30 ≤ n < 100: probe rate = 1/5
      - n ≥ 100: probe rate = 1/10
    """

    def test_below_min_n_full_probe(self):
        assert probe_rate_for_n(0) == 1.0
        assert probe_rate_for_n(9) == 1.0

    def test_low_n_high_probe(self):
        assert probe_rate_for_n(10) == pytest.approx(1.0 / 3.0)
        assert probe_rate_for_n(29) == pytest.approx(1.0 / 3.0)

    def test_mid_n_moderate_probe(self):
        assert probe_rate_for_n(30) == pytest.approx(1.0 / 5.0)
        assert probe_rate_for_n(99) == pytest.approx(1.0 / 5.0)

    def test_high_n_low_probe(self):
        assert probe_rate_for_n(100) == pytest.approx(1.0 / 10.0)
        assert probe_rate_for_n(1000) == pytest.approx(1.0 / 10.0)


class TestShouldProbe:
    """Deterministic probe selection: hash of (fingerprint, today) →
    bool. Same finding probes consistently within a day; over many days
    the probe rate converges to ``probe_rate_for_n``."""

    def test_deterministic_within_day(self):
        a = should_probe("abc123", date_iso="2026-04-27", probe_rate=0.5)
        b = should_probe("abc123", date_iso="2026-04-27", probe_rate=0.5)
        assert a == b

    def test_different_fingerprints_distribute(self):
        rate = 0.2
        results = [
            should_probe(f"fp{i:04d}", date_iso="2026-04-27", probe_rate=rate)
            for i in range(2000)
        ]
        observed = sum(results) / len(results)
        assert abs(observed - rate) < 0.05

    def test_zero_rate_never_probes(self):
        assert should_probe("abc", date_iso="2026-04-27", probe_rate=0.0) is False

    def test_one_rate_always_probes(self):
        assert should_probe("abc", date_iso="2026-04-27", probe_rate=1.0) is True
