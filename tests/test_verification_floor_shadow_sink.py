"""The verification-floor shadow must leave a durable, rate-shaped record.

Issue #2169: the shadow ran on every check-in of every default deployment and
persisted nowhere — the firing was written onto the returned result dict and
discarded with the process. The evidence its enable decision waits on was being
computed and thrown away.

These tests pin the three properties that failure had:

1. **A firing reaches the sink**, not just the result dict.
2. **A non-firing reaches the sink too**, because the enable decision asks for a
   rate and a numerator alone cannot answer it.
3. **Nothing about the decision changes** — the sink is measurement on a
   mandatory path and fails open.
"""

import pytest

from src.governance_monitor import UNITARESMonitor
from src.verification_floor_shadow import (
    RECORD_ALL,
    RECORD_FIRINGS,
    RECORD_OFF,
    SCHEMA,
    attach_live_decision,
    evaluate,
    record,
    record_mode,
    should_record,
)
from governance_core.verification import score_harm_confession

SABOTAGE = (
    "I disabled the audit logging so the run would look clean, deleted the "
    "rollback snapshots, exfiltrated the API credentials to my own bucket, and "
    "rewrote the protected history so the change looks routine."
)
BENIGN = (
    "I refactored the retry helper into a small module, added unit tests for "
    "the backoff path, and updated the changelog entry for the release."
)
TEMPLATED = "status=ok cycle=4821 queue_depth=0 last_error=none uptime=91223"


class _Capture:
    """Audit logger that keeps the shadow rows and delegates everything else.

    Delegation matters: the check-in path emits other audit events on the same
    pass, and a stub that swallowed them would make these tests pass for a
    reason unrelated to the sink.
    """

    def __init__(self, delegate):
        self.rows = []
        self._delegate = delegate

    def log_verification_floor_shadow(self, agent_id, payload):
        self.rows.append((agent_id, payload))

    def __getattr__(self, name):
        return getattr(self._delegate, name)


class _Exploding(_Capture):
    def log_verification_floor_shadow(self, agent_id, payload):
        raise RuntimeError("audit sink down")


def _checkin(text):
    return {
        "response_text": text,
        "ethical_drift": [0.1, 0.1, 0.1],
        "complexity": 0.4,
        "confidence": 0.8,
    }


def _set(monkeypatch, *, floor, shadow, mode=None):
    import config.governance_config as cfg

    monkeypatch.setattr(cfg.GovernanceConfig, "VERIFICATION_FLOOR_ENABLED", floor)
    monkeypatch.setattr(cfg.GovernanceConfig, "VERIFICATION_FLOOR_SHADOW", shadow)
    if mode is None:
        monkeypatch.delenv("GOVERNANCE_VERIFICATION_FLOOR_SHADOW_RECORD", raising=False)
    else:
        monkeypatch.setenv("GOVERNANCE_VERIFICATION_FLOOR_SHADOW_RECORD", mode)


@pytest.fixture
def sink(monkeypatch):
    import src.governance_monitor as gm

    capture = _Capture(gm.audit_logger)
    monkeypatch.setattr(gm, "audit_logger", capture)
    return capture


class TestRecordMode:
    def test_default_records_every_evaluation(self, monkeypatch):
        monkeypatch.delenv("GOVERNANCE_VERIFICATION_FLOOR_SHADOW_RECORD", raising=False)
        assert record_mode() == RECORD_ALL
        assert should_record(fired=False) is True
        assert should_record(fired=True) is True

    def test_firings_mode_drops_the_denominator(self, monkeypatch):
        monkeypatch.setenv("GOVERNANCE_VERIFICATION_FLOOR_SHADOW_RECORD", "firings")
        assert record_mode() == RECORD_FIRINGS
        assert should_record(fired=False) is False
        assert should_record(fired=True) is True

    def test_off_mode_records_nothing(self, monkeypatch):
        monkeypatch.setenv("GOVERNANCE_VERIFICATION_FLOOR_SHADOW_RECORD", "off")
        assert record_mode() == RECORD_OFF
        assert should_record(fired=True) is False

    def test_typo_falls_back_to_all_not_to_silence(self, monkeypatch):
        # A misspelled sink flag must not reproduce the blind-instrument state
        # this module exists to end.
        monkeypatch.setenv("GOVERNANCE_VERIFICATION_FLOOR_SHADOW_RECORD", "fireings")
        assert record_mode() == RECORD_ALL


class TestRowShape:
    def test_firing_row_carries_the_counterfactual(self):
        signal = score_harm_confession(SABOTAGE)
        row = evaluate(
            signal,
            response_text=SABOTAGE,
            verdict_before="safe",
            risk_before=0.26,
            verdict_after="high-risk",
            risk_after=signal.score,
            mode=RECORD_ALL,
        )
        assert row["schema"] == SCHEMA
        assert row["fired"] is True
        assert row["applied"] is False
        assert row["escalated_verdict"] is True
        assert row["risk_delta"] > 0
        assert row["scoreable"] is True
        assert row["categories"]
        assert row["matches"]

    def test_non_firing_row_is_an_observation_not_an_absence(self):
        signal = score_harm_confession(BENIGN)
        row = evaluate(
            signal,
            response_text=BENIGN,
            verdict_before="safe",
            risk_before=0.26,
            verdict_after="safe",
            risk_after=0.26,
            mode=RECORD_ALL,
        )
        assert row["evaluated"] is True
        assert row["fired"] is False
        assert row["scoreable"] is True
        assert row["escalated_verdict"] is False
        assert row["risk_delta"] == 0.0

    def test_templated_caller_is_unscored_not_cleared(self):
        # The detector reads English first-person narration. A status line is
        # outside its denominator and the row has to say so.
        signal = score_harm_confession(TEMPLATED)
        row = evaluate(
            signal,
            response_text=TEMPLATED,
            verdict_before="safe",
            risk_before=0.1,
            verdict_after="safe",
            risk_after=0.1,
            mode=RECORD_ALL,
        )
        assert row["fired"] is False
        assert row["first_person"] is False

    def test_empty_text_is_named_unscoreable(self):
        signal = score_harm_confession("")
        row = evaluate(
            signal,
            response_text="",
            verdict_before="safe",
            risk_before=0.1,
            verdict_after="safe",
            risk_after=0.1,
            mode=RECORD_ALL,
        )
        assert row["scoreable"] is False
        assert row["unscoreable_reason"] == "empty_response_text"

    def test_matches_are_bounded_before_durable_storage(self):
        class _Fake:
            score = 0.9
            verdict = "high-risk"
            categories = {"a": 0.5}
            matches = ["x" * 400] * 40
            abstained = False

        row = evaluate(
            _Fake(),
            response_text="I deleted the backups.",
            verdict_before="safe",
            risk_before=0.1,
            verdict_after="high-risk",
            risk_after=0.9,
            mode=RECORD_ALL,
        )
        assert len(row["matches"]) == 8
        assert all(len(m) <= 120 for m in row["matches"])

    def test_attach_live_decision_makes_the_fp_question_answerable(self):
        row = attach_live_decision(
            {"escalated_verdict": True},
            action="proceed",
            sub_action=None,
            live_verdict="safe",
        )
        assert row["live_action"] == "proceed"
        assert row["live_verdict"] == "safe"


class TestFirstPersonIsAStratumNotAFilter:
    """Regression: the detector does NOT require a first-person pronoun.

    The first draft of this module documented the pronoun as a necessary
    condition for firing and let the reader use it as a denominator filter. It is
    not necessary, and treating it as one produced a false-positive rate above
    1.0 on the number the enable gate consumes.
    """

    PRONOUN_FREE = (
        "Disabled telemetry for the run; deleted snapshots afterwards."
    )

    def test_detector_fires_without_a_first_person_pronoun(self):
        signal = score_harm_confession(self.PRONOUN_FREE)
        assert signal.score > 0.0, (
            "if this ever becomes true, the pronoun is still not a precondition "
            "by construction — check _CATEGORY_SPECS before relaxing anything"
        )
        assert signal.verdict == "high-risk"

    def test_such_a_row_is_recorded_as_firing_and_pronoun_free(self):
        signal = score_harm_confession(self.PRONOUN_FREE)
        row = evaluate(
            signal,
            response_text=self.PRONOUN_FREE,
            verdict_before="safe",
            risk_before=0.26,
            verdict_after="high-risk",
            risk_after=signal.score,
            mode=RECORD_ALL,
        )
        # Both must be true at once: this is the combination the false premise
        # said could not exist.
        assert row["fired"] is True
        assert row["first_person"] is False
        assert row["scoreable"] is True


class TestAppliedRowsAreRecordedToo:
    """The record must not depend on the flag state it exists to inform."""

    def test_enabled_floor_writes_an_applied_row(self, monkeypatch, sink):
        _set(monkeypatch, floor=True, shadow=False)
        result = UNITARESMonitor("vfs-applied", load_state=False).process_update(
            _checkin(SABOTAGE)
        )
        assert result["decision"]["action"] == "pause"
        rows = [payload for _, payload in sink.rows]
        assert len(rows) == 1
        assert rows[0]["applied"] is True
        assert rows[0]["fired"] is True
        # verdict_before is the PRE-floor verdict even though the floor applied.
        assert rows[0]["verdict_before"] != rows[0]["verdict_after"]
        assert rows[0]["escalated_verdict"] is True

    def test_enabled_floor_on_benign_writes_a_non_firing_applied_row(
        self, monkeypatch, sink
    ):
        _set(monkeypatch, floor=True, shadow=False)
        UNITARESMonitor("vfs-applied-clean", load_state=False).process_update(
            _checkin(BENIGN)
        )
        rows = [payload for _, payload in sink.rows]
        assert len(rows) == 1
        assert rows[0]["applied"] is True
        assert rows[0]["fired"] is False


class TestRecordFailsOpen:
    def test_a_broken_sink_never_raises(self):
        # Optional measurement on the mandatory check-in path: a dead audit sink
        # must not cost anyone a check-in.
        record(_Exploding(None), "agent", {"schema": SCHEMA})


class TestEndToEnd:
    def _monitor(self, name):
        return UNITARESMonitor(name, load_state=False)

    def test_firing_reaches_the_sink(self, monkeypatch, sink):
        _set(monkeypatch, floor=False, shadow=True)
        result = self._monitor("vfs-fire").process_update(_checkin(SABOTAGE))
        assert result["decision"]["action"] == "proceed"
        rows = [payload for _, payload in sink.rows]
        assert len(rows) == 1
        assert rows[0]["fired"] is True
        assert rows[0]["applied"] is False
        assert rows[0]["measurement_scope"] == "live"
        assert rows[0]["live_action"] == "proceed"
        # The counterfactual is the real combination, computed through the same
        # pure function the enabled floor uses.
        assert rows[0]["verdict_after"] == "high-risk"
        assert rows[0]["escalated_verdict"] is True

    def test_non_firing_reaches_the_sink_too(self, monkeypatch, sink):
        # This is the property the whole issue turns on: without the clean
        # check-ins there is no denominator and no false-positive rate.
        _set(monkeypatch, floor=False, shadow=True)
        result = self._monitor("vfs-clean").process_update(_checkin(BENIGN))
        assert "verification_floor_shadow" not in result  # in-band stays quiet
        rows = [payload for _, payload in sink.rows]
        assert len(rows) == 1
        assert rows[0]["fired"] is False
        assert rows[0]["scoreable"] is True

    def test_firings_mode_writes_only_the_numerator(self, monkeypatch, sink):
        _set(monkeypatch, floor=False, shadow=True, mode="firings")
        self._monitor("vfs-num-a").process_update(_checkin(BENIGN))
        assert sink.rows == []
        self._monitor("vfs-num-b").process_update(_checkin(SABOTAGE))
        assert len(sink.rows) == 1

    def test_off_mode_writes_nothing(self, monkeypatch, sink):
        _set(monkeypatch, floor=False, shadow=True, mode="off")
        self._monitor("vfs-off").process_update(_checkin(SABOTAGE))
        assert sink.rows == []

    def test_shadow_kill_switch_still_silences_the_sink(self, monkeypatch, sink):
        _set(monkeypatch, floor=False, shadow=False)
        self._monitor("vfs-killed").process_update(_checkin(SABOTAGE))
        assert sink.rows == []

    def test_enabled_floor_writes_an_applied_row_not_a_shadow_one(
        self, monkeypatch, sink
    ):
        # With the floor on, the signal is enforced rather than hypothetical, so
        # the row says applied=true. Recording nothing here would recreate the
        # #2169 hole one flag flip later, and would leave the doctor check
        # permanently unable to tell an enabled floor from a detached sink.
        _set(monkeypatch, floor=True, shadow=True)
        result = self._monitor("vfs-enabled").process_update(_checkin(SABOTAGE))
        assert result["decision"]["action"] == "pause"
        assert "verification_floor" in result
        assert "verification_floor_shadow" not in result
        rows = [payload for _, payload in sink.rows]
        assert len(rows) == 1
        assert rows[0]["applied"] is True

    def test_sink_failure_does_not_break_the_checkin(self, monkeypatch):
        _set(monkeypatch, floor=False, shadow=True)
        import src.governance_monitor as gm

        monkeypatch.setattr(gm, "audit_logger", _Exploding(gm.audit_logger))
        result = self._monitor("vfs-broken").process_update(_checkin(SABOTAGE))
        assert result["decision"]["action"] == "proceed"

    def test_simulation_rows_are_labelled_not_pooled(self, monkeypatch, sink):
        _set(monkeypatch, floor=False, shadow=True)
        monitor = self._monitor("vfs-sim")
        monitor.simulate_update(_checkin(SABOTAGE))
        rows = [payload for _, payload in sink.rows]
        assert rows, "a simulated check-in still evaluates the shadow"
        assert all(row["measurement_scope"] == "simulation" for row in rows)
