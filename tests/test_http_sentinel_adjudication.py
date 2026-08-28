"""Tests for the Sentinel adjudication endpoints (dashboard widget backend).

A minimal Starlette app mounts just the two routes; DB helpers and the inline
outcome recorder are patched so no live governance stack is needed. What's
under test: the operator write gate, input validation, idempotency, and that
a verdict produces exactly the outcome args the CLI path would (shared
builder semantics — fp dismissal is the only bad label).
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import src.http_routes.sentinel as sentinel_routes

from src.http_api import (
    http_sentinel_adjudicate,
    http_sentinel_adjudication_queue,
)

OP_TOKEN = "test-operator-token"
READ_TOKEN = "test-dashboard-read-token"
SENTINEL_UUID = "f92dcea8-4786-412a-a0eb-362c273382f5"
PROGRESS = {"outcomes": 25, "bad": 3, "days": 4, "bad_days": 1, "bad_days_target": 3}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", OP_TOKEN)
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", READ_TOKEN)
    app = Starlette(routes=[
        Route("/v1/sentinel/adjudication-queue", http_sentinel_adjudication_queue, methods=["GET"]),
        Route("/v1/sentinel/adjudicate", http_sentinel_adjudicate, methods=["POST"]),
    ])
    return TestClient(app, headers={"Authorization": f"Bearer {READ_TOKEN}"})


def _op_headers():
    return {"X-Unitares-Operator": OP_TOKEN}


# ---------------------------------------------------------------------------
# POST /v1/sentinel/adjudicate
# ---------------------------------------------------------------------------

class TestAdjudicateGate:
    def test_no_operator_header_is_403(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fp1", "status": "confirmed"})
        assert r.status_code == 403
        assert "operator" in r.json()["error"].lower()

    def test_wrong_operator_token_is_403(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fp1", "status": "confirmed"},
                        headers={"X-Unitares-Operator": "not-the-token"})
        assert r.status_code == 403


class TestAdjudicateValidation:
    def test_missing_fingerprint_400(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"status": "confirmed"}, headers=_op_headers())
        assert r.status_code == 400

    def test_bad_status_400(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fp1", "status": "maybe"},
                        headers=_op_headers())
        assert r.status_code == 400

    def test_dismissal_without_reason_400(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fp1", "status": "dismissed"},
                        headers=_op_headers())
        assert r.status_code == 400
        assert "reason" in r.json()["error"]


class TestAdjudicateRecording:
    def _patches(self, already=frozenset(), uuid=SENTINEL_UUID):
        return (
            patch("src.http_routes.sentinel._adjudicated_sentinel_fingerprints",
                  AsyncMock(return_value=set(already))),
            patch("src.http_routes.sentinel._sentinel_substrate_uuid",
                  AsyncMock(return_value=uuid)),
            # Attribution now resolves the PRODUCER from the finding row.
            # These cases exercise recording semantics, so stand in as a
            # Sentinel-slug producer -- the path that still falls back to the
            # substrate claim, keeping the pre-existing assertions meaningful.
            patch("src.http_routes.sentinel._finding_producer_uuid",
                  AsyncMock(return_value=(None, "sentinel", "sentinel_alarm_finding"))),
            patch("src.http_routes.sentinel._adjudication_progress",
                  AsyncMock(return_value=dict(PROGRESS))),
            patch("src.mcp_handlers.observability.outcome_events._record_outcome_event_inline",
                  AsyncMock(return_value={"success": True})),
        )

    def test_fp_dismissal_records_bad_label(self, client):
        p1, p2, prod, p3, rec = self._patches()
        with p1, p2, p3, prod, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp1", "status": "dismissed", "reason": "fp"},
                            headers=_op_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["outcome_type"] == "sentinel_finding_dismissed"
        assert body["is_bad"] is True
        assert body["progress"] == PROGRESS
        args = recorder.call_args[0][0]
        assert args["agent_id"] == SENTINEL_UUID
        assert args["verification_source"] == "external_signal"
        assert args["detail"]["fingerprint"] == "fp1"
        assert args["detail"]["adjudicated_via"] == "dashboard"

    def test_confirmation_is_good_label(self, client):
        p1, p2, prod, p3, rec = self._patches()
        with p1, p2, p3, prod, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp2", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 200
        assert r.json()["outcome_type"] == "sentinel_finding_confirmed"
        assert r.json()["is_bad"] is False
        assert recorder.call_args[0][0]["is_bad"] is False

    def test_non_fp_dismissal_is_not_bad(self, client):
        p1, p2, prod, p3, rec = self._patches()
        with p1, p2, p3, prod, rec:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp3", "status": "dismissed",
                                  "reason": "out_of_scope"},
                            headers=_op_headers())
        assert r.status_code == 200
        assert r.json()["is_bad"] is False

    def test_double_adjudication_409(self, client):
        p1, p2, prod, p3, rec = self._patches(already={"fp1"})
        with p1, p2, p3, prod, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp1", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 409
        recorder.assert_not_called()

    def test_missing_substrate_claim_503(self, client):
        p1, p2, prod, p3, rec = self._patches(uuid=None)
        with p1, p2, p3, prod, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp1", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 503
        recorder.assert_not_called()


# ---------------------------------------------------------------------------
# GET /v1/sentinel/adjudication-queue
# ---------------------------------------------------------------------------

def _event(fp, severity="high", msg="m", ts="2026-07-01T00:00:00+00:00"):
    return {"timestamp": ts, "agent_id": "sentinel", "event_id": fp + "-ev",
            "details": {"severity": severity, "finding_type": "ad_hoc",
                        "message": msg, "fingerprint": fp, "agent_name": "Sentinel"}}


class TestAdjudicationQueue:
    def test_filters_adjudicated_dedupes_and_counts(self, client):
        events = [
            _event("fp-a"),                      # pending
            _event("fp-a"),                      # duplicate fingerprint -> deduped
            _event("fp-done"),                   # already adjudicated -> excluded
            _event("fp-b", severity="critical"), # pending
            _event("fp-low", severity="low"),    # below default severities -> excluded
            _event("fp-x") | {"details": {"severity": "high", "message": "no fp"}},  # no fingerprint
        ]
        with patch("src.audit_db.query_audit_events_async",
                   AsyncMock(return_value=events)), \
             patch("src.http_routes.sentinel._adjudicated_sentinel_fingerprints",
                   AsyncMock(return_value={"fp-done"})), \
             patch("src.http_routes.sentinel._adjudication_progress",
                   AsyncMock(return_value=dict(PROGRESS))):
            r = client.get("/v1/sentinel/adjudication-queue?limit=5")
        assert r.status_code == 200
        body = r.json()
        fps = [q["fingerprint"] for q in body["queue"]]
        assert fps == ["fp-a", "fp-b"]
        assert body["pending_total"] == 2
        assert body["progress"] == PROGRESS
        assert "fp" in body["dismiss_reasons"]

    def test_limit_caps_queue_but_not_pending_total(self, client):
        events = [_event(f"fp-{i}") for i in range(8)]
        with patch("src.audit_db.query_audit_events_async",
                   AsyncMock(return_value=events)), \
             patch("src.http_routes.sentinel._adjudicated_sentinel_fingerprints",
                   AsyncMock(return_value=set())), \
             patch("src.http_routes.sentinel._adjudication_progress",
                   AsyncMock(return_value=dict(PROGRESS))):
            r = client.get("/v1/sentinel/adjudication-queue?limit=3")
        body = r.json()
        assert len(body["queue"]) == 3
        assert body["pending_total"] == 8


# ---------------------------------------------------------------------------
# Forced-release event-check enrichment (bridge-dispatch proposal §4, PR #1450)
# ---------------------------------------------------------------------------

from decimal import Decimal  # noqa: E402

from src.http_api import (  # noqa: E402
    _assess_forced_release_row,
    _finding_report_latency_s,
)

LEASE_ID = "17546f52-370d-4274-9d5b-0c233f09590c"
LEASE_ID_2 = "a2d89680-0f3a-48e5-b51a-50684f70bfd9"


def _lease_row(**over):
    row = {
        "lease_id": LEASE_ID,
        "surface_id": "resident:/sentinel_cycle",
        "release_reason": "forced",
        "holder_kind": "local_beam",
        "holder_pid_null": True,
        "original_ttl_s": 300,
        # asyncpg returns EXTRACT(epoch ...) as Decimal on the live driver —
        # mirror that so the float() coercion stays covered.
        "held_s": Decimal("4350.0"),  # 14.5x TTL
    }
    row.update(over)
    return row


class TestForcedReleaseAssessment:
    def test_missing_row_is_lease_plane_integrity_state(self):
        ev = _assess_forced_release_row(None, "resident:/sentinel_cycle")
        assert ev["assessment"] == "no_lease_row"

    def test_matching_row_is_event_recorded_with_facts(self):
        ev = _assess_forced_release_row(_lease_row(), "resident:/sentinel_cycle")
        assert ev["assessment"] == "event_recorded"
        assert ev["held_x_ttl"] == 14.5
        assert ev["holder_pid_null"] is True

    def test_surface_mismatch_is_lookup_mismatch_not_contradiction(self):
        ev = _assess_forced_release_row(_lease_row(), "resident:/steward")
        assert ev["assessment"] == "lookup_mismatch"
        assert ev["surface_match"] is False

    def test_unforced_release_reason_is_lookup_mismatch(self):
        ev = _assess_forced_release_row(
            _lease_row(release_reason="normal"), "resident:/sentinel_cycle")
        assert ev["assessment"] == "lookup_mismatch"

    def test_null_held_s_yields_no_ratio(self):
        ev = _assess_forced_release_row(
            _lease_row(held_s=None), "resident:/sentinel_cycle")
        assert ev["assessment"] == "event_recorded"
        assert ev["held_x_ttl"] is None


class TestReportLatency:
    def test_z_suffix_and_offset_formats_both_parse(self):
        s = _finding_report_latency_s(
            "2026-07-31T13:16:47.795293+00:00", "2026-07-31T05:53:46Z")
        assert s is not None and 26580 < s < 26582  # the documented 7.4h lag

    def test_unparseable_inputs_yield_none(self):
        assert _finding_report_latency_s(None, "2026-07-31T05:53:46Z") is None
        assert _finding_report_latency_s("not-a-ts", "also-not") is None

    def test_clock_skew_clamps_to_zero(self):
        assert _finding_report_latency_s(
            "2026-07-31T05:00:00Z", "2026-07-31T06:00:00Z") == 0.0


def _fr_event(fp, lease_id=LEASE_ID, surface="resident:/sentinel_cycle",
              event_ts="2026-07-31T21:08:05Z", ts="2026-07-31T21:08:31+00:00"):
    e = _event(fp, msg=f"forced release: {surface} (lease {lease_id})", ts=ts)
    e["details"].update({"lease_id": lease_id, "surface_id": surface, "ts": event_ts})
    return e


class TestQueueEvidenceEnrichment:
    def _get_queue(self, client, events, lease_rows):
        with patch("src.audit_db.query_audit_events_async",
                   AsyncMock(return_value=events)), \
             patch("src.http_routes.sentinel._adjudicated_sentinel_fingerprints",
                   AsyncMock(return_value=set())), \
             patch("src.http_routes.sentinel._adjudication_progress",
                   AsyncMock(return_value=dict(PROGRESS))), \
             patch("src.http_routes.sentinel._fetch_lease_rows", lease_rows):
            return client.get("/v1/sentinel/adjudication-queue?limit=5")

    def test_evidence_attached_only_to_forced_release_findings(self, client):
        events = [_fr_event("fp-fr"), _event("fp-plain")]
        r = self._get_queue(
            client, events, AsyncMock(return_value={LEASE_ID: _lease_row()}))
        assert r.status_code == 200
        q = r.json()["queue"]
        assert q[0]["evidence"]["assessment"] == "event_recorded"
        assert q[0]["evidence"]["report_latency_s"] == 26.0
        assert "evidence" not in q[1]

    def test_malformed_lease_id_cannot_poison_the_batch(self, client):
        # 36 hyphens satisfies a loose shape check but not the uuid cast; it
        # must degrade only its own finding, never the whole page.
        events = [_fr_event("fp-bad", lease_id="-" * 36),
                  _fr_event("fp-good", lease_id=LEASE_ID)]
        fetch = AsyncMock(return_value={LEASE_ID: _lease_row()})
        r = self._get_queue(client, events, fetch)
        q = r.json()["queue"]
        assert q[0]["evidence"]["assessment"] == "lookup_mismatch"
        assert q[1]["evidence"]["assessment"] == "event_recorded"
        (called_ids,) = fetch.call_args.args
        assert called_ids == [LEASE_ID]  # malformed id never reaches SQL

    def test_missing_lease_id_key_is_lookup_mismatch(self, client):
        e = _fr_event("fp-nolid")
        del e["details"]["lease_id"]
        r = self._get_queue(client, [e], AsyncMock(return_value={}))
        ev = r.json()["queue"][0]["evidence"]
        assert ev["assessment"] == "lookup_mismatch"
        assert "no lease id" in ev.get("note", "")

    def test_fetch_failure_is_explicit_check_error_not_silence(self, client):
        events = [_fr_event("fp-fr")]
        r = self._get_queue(
            client, events, AsyncMock(side_effect=RuntimeError("db down")))
        assert r.status_code == 200
        q = r.json()["queue"]
        assert len(q) == 1
        assert q[0]["evidence"]["assessment"] == "check_error"


class TestProducerAttribution:
    """`build_resolution_outcome_args` states the contract: "agent_uuid must be
    the resident's own UUID so the handler snapshots that resident's EISV."
    The endpoint passed Sentinel's UUID unconditionally — correct only while
    the queue is Sentinel-only, and the landmine under any widening.
    """

    def _patches(self, producer, uuid=SENTINEL_UUID, already=frozenset()):
        return (
            patch("src.http_routes.sentinel._adjudicated_sentinel_fingerprints",
                  AsyncMock(return_value=set(already))),
            patch("src.http_routes.sentinel._sentinel_substrate_uuid",
                  AsyncMock(return_value=uuid)),
            patch("src.http_routes.sentinel._finding_producer_uuid",
                  AsyncMock(return_value=producer)),
            patch("src.http_routes.sentinel._adjudication_progress",
                  AsyncMock(return_value=dict(PROGRESS))),
            patch("src.mcp_handlers.observability.outcome_events._record_outcome_event_inline",
                  AsyncMock(return_value={"success": True})),
        )

    def test_outcome_is_booked_against_the_producer_not_sentinel(self, client):
        """The whole point: a Watcher finding must not credit Sentinel."""
        watcher = "907e3195-c649-49db-b753-1edc1a105f33"
        p1, p2, prod, p3, rec = self._patches(producer=(watcher, watcher, "watcher_finding"))
        with p1, p2, prod, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp9", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 200
        args = recorder.call_args[0][0]
        assert args["agent_id"] == watcher
        assert args["agent_id"] != SENTINEL_UUID

    def test_sentinel_slug_still_falls_back_to_the_substrate_claim(self, client):
        """Sentinel writes the bare slug 'sentinel' on its alarm findings (249
        rows/14d), so that path must keep working byte-identically."""
        p1, p2, prod, p3, rec = self._patches(producer=(None, "sentinel", "sentinel_alarm_finding"))
        with p1, p2, prod, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp8", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 200
        assert recorder.call_args[0][0]["agent_id"] == SENTINEL_UUID

    def test_unattributable_producer_is_refused_not_misbooked(self, client):
        """A doctor finding has no governance identity. Refusing is correct;
        booking it against Sentinel would corrupt the anchor channel the
        falsifiability test depends on."""
        p1, p2, prod, p3, rec = self._patches(producer=(None, "doctor-findings", "doctor_check_finding"))
        with p1, p2, prod, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp7", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 422
        assert "doctor-findings" in r.json()["error"]
        recorder.assert_not_called()

    def test_unknown_fingerprint_is_refused(self, client):
        p1, p2, prod, p3, rec = self._patches(producer=(None, None, None))
        with p1, p2, prod, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "nope", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 422
        recorder.assert_not_called()

    def test_producer_ref_is_recorded_for_forensics(self, client):
        watcher = "907e3195-c649-49db-b753-1edc1a105f33"
        p1, p2, prod, p3, rec = self._patches(producer=(watcher, watcher, "watcher_finding"))
        with p1, p2, prod, p3, rec as recorder:
            client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fp6", "status": "confirmed"},
                        headers=_op_headers())
        assert recorder.call_args[0][0]["detail"]["producer_ref"] == watcher

    def test_outcome_type_is_unchanged_so_dedup_still_matches(self, client):
        """outcome_type prefixes feed _SENTINEL_ADJUDICATION_OUTCOME_TYPES and
        _adjudication_progress. Per-producer types must land WITH those
        filters — changing them here would silently break dedup."""
        watcher = "907e3195-c649-49db-b753-1edc1a105f33"
        p1, p2, prod, p3, rec = self._patches(producer=(watcher, watcher, "watcher_finding"))
        with p1, p2, prod, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp5", "status": "confirmed"},
                            headers=_op_headers())
        assert r.json()["outcome_type"] == "sentinel_finding_confirmed"
        from src.http_routes.sentinel import _SENTINEL_ADJUDICATION_OUTCOME_TYPES
        assert r.json()["outcome_type"] in _SENTINEL_ADJUDICATION_OUTCOME_TYPES


class TestDoctorFamilyWidening:
    """doctor_check_finding joined the queue on 2026-08-26.

    It could not before: all 203 doctor findings in the preceding 30 days wrote
    the bare slug 'doctor-findings' into audit.events.agent_id, so
    _finding_producer_uuid could not resolve one and every adjudication
    attempt returned 422. Provisioning the doctor layer's shared identity is
    what made the family eligible; these pin what widening must and must not do.
    """

    DOCTOR_UUID = "7dea7dcb-e887-4c90-8c8a-4f3433da102b"

    def _patches(self, producer, already=frozenset()):
        return (
            patch("src.http_routes.sentinel._adjudicated_sentinel_fingerprints",
                  AsyncMock(return_value=set(already))),
            patch("src.http_routes.sentinel._sentinel_substrate_uuid",
                  AsyncMock(return_value=SENTINEL_UUID)),
            patch("src.http_routes.sentinel._finding_producer_uuid",
                  AsyncMock(return_value=producer)),
            patch("src.http_routes.sentinel._adjudication_progress",
                  AsyncMock(return_value=dict(PROGRESS))),
            patch("src.mcp_handlers.observability.outcome_events._record_outcome_event_inline",
                  AsyncMock(return_value={"success": True})),
        )

    def test_doctor_finding_is_in_the_queue_families(self):
        assert "doctor_check_finding" in sentinel_routes._SENTINEL_FINDING_EVENT_TYPES

    def test_doctor_outcome_keeps_its_own_label(self, client):
        """⛔The identities pool; the LABELS must not.

        These detectors differ enormously in precision and volume, so one
        shared outcome_type would track the volume mix rather than any
        detector's quality — the confound that made the pooled
        dialectic-reviewer number describe neither instrument.
        """
        p1, p2, prod, p3, rec = self._patches(
            producer=(self.DOCTOR_UUID, self.DOCTOR_UUID, "doctor_check_finding"))
        with p1, p2, prod, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fpdoc1", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 200
        args = recorder.call_args[0][0]
        assert args["outcome_type"] == "doctor_check_finding_confirmed"
        assert args["agent_id"] == self.DOCTOR_UUID

    def test_sentinel_label_is_unchanged_by_the_widening(self, client):
        """Both Sentinel families still map to 'sentinel_finding'.

        Remapping them would orphan every historical sentinel_finding_* row
        from the dedup set that has to find it again.
        """
        p1, p2, prod, p3, rec = self._patches(
            producer=(SENTINEL_UUID, SENTINEL_UUID, "sentinel_alarm_finding"))
        with p1, p2, prod, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fpsen1", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 200
        assert recorder.call_args[0][0]["outcome_type"] == "sentinel_finding_confirmed"

    def test_dismissal_reason_still_drives_is_bad_per_family(self, client):
        """Only 'fp' is a bad label — the sole true-negative in precision math."""
        p1, p2, prod, p3, rec = self._patches(
            producer=(self.DOCTOR_UUID, self.DOCTOR_UUID, "doctor_check_finding"))
        with p1, p2, prod, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fpdoc2", "status": "dismissed",
                                  "reason": "fp"},
                            headers=_op_headers())
        assert r.status_code == 200
        args = recorder.call_args[0][0]
        assert args["outcome_type"] == "doctor_check_finding_dismissed"
        assert args["is_bad"] is True

    def test_dedup_set_covers_every_queue_family(self):
        """⛔A family missing here is adjudicated, recorded, and then handed
        straight back to the operator on the next page load."""
        kinds = set(sentinel_routes._FINDING_KIND_BY_EVENT_TYPE.values())
        for kind in kinds:
            for suffix in ("confirmed", "dismissed"):
                assert f"{kind}_{suffix}" in sentinel_routes._SENTINEL_ADJUDICATION_OUTCOME_TYPES
        assert "doctor_check_finding_confirmed" in sentinel_routes._SENTINEL_ADJUDICATION_OUTCOME_TYPES

    def test_every_queue_family_has_a_label_mapping(self):
        """A family in the queue with no mapping would silently book its
        outcomes under Sentinel's label via the default."""
        for event_type in sentinel_routes._SENTINEL_FINDING_EVENT_TYPES:
            assert event_type in sentinel_routes._FINDING_KIND_BY_EVENT_TYPE

    def test_falsifier_progress_is_not_silently_widened(self):
        """⛔Deliberate: _adjudication_progress reads the EISV falsifier's
        anchor-day count, an externally quoted figure. Whether a doctor
        adjudication is anchor evidence of the same grade as a Sentinel one is
        an open operator call — not a side effect of closing the doctor loop.
        """
        src = inspect.getsource(sentinel_routes._adjudication_progress)
        assert "doctor_check_finding" not in src
        assert "sentinel_finding_%" in src


# ---------------------------------------------------------------------------
# Abstention — "I cannot determine this" is not a verdict
# ---------------------------------------------------------------------------
#
# Context. audit.outcome_events declares `is_bad BOOLEAN NOT NULL` (migration
# 004), so that table cannot represent an absence of judgement — every path out
# of the queue asserted a truth value. Measured 2026-08-28, the record shows
# what that produced: sentinel_finding is 17 confirmed and 0 dismissed, ever,
# which is the all-positive generator the invariant on
# _SENTINEL_FINDING_EVENT_TYPES forbids. Abstention gives the queue a zero-cost
# exit that reaches neither is_bad, the falsifier, nor the ablation matrix.

class TestAbstention:
    def _patches(self, already=frozenset()):
        return (
            patch("src.http_routes.sentinel._adjudicated_sentinel_fingerprints",
                  AsyncMock(return_value=set(already))),
            patch("src.mcp_handlers.observability.outcome_events._record_outcome_event_inline",
                  AsyncMock(return_value={"success": True})),
        )

    def test_abstain_writes_no_outcome_event(self, client):
        """The load-bearing assertion: nothing enters the anchor channel."""
        adjudicated, recorder = self._patches()
        appended = AsyncMock(return_value=True)
        with adjudicated, recorder as rec, \
                patch("src.db.get_db", return_value=type("DB", (), {"append_audit_event": appended})()):
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fpA", "status": "abstain"},
                            headers=_op_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["recorded_outcome"] is False
        # No outcome_event, therefore no is_bad, therefore no anchor row.
        rec.assert_not_awaited()

    def test_abstain_records_a_non_label_audit_event(self, client):
        adjudicated, recorder = self._patches()
        appended = AsyncMock(return_value=True)
        with adjudicated, recorder, \
                patch("src.db.get_db", return_value=type("DB", (), {"append_audit_event": appended})()):
            client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fpB", "status": "abstain"},
                        headers=_op_headers())
        event = appended.call_args[0][0]
        assert event.event_type == sentinel_routes._ADJUDICATION_ABSTAIN_EVENT_TYPE
        assert event.payload["fingerprint"] == "fpB"
        # The row says what it is, so a later reader cannot mistake it for a
        # verdict that merely lacks a label.
        assert "NOT a verdict" in event.payload["note"]

    def test_abstain_event_type_cannot_reach_the_anchor_channel(self):
        """Isolation is structural, not incidental.

        The abstain event type must stay out of the finding families (or the
        queue would re-ingest it as a finding) and out of the adjudication
        outcome types (or _adjudication_progress and the 409 dedup would count
        it). Both are compile-time tuples, so assert on them directly.
        """
        t = sentinel_routes._ADJUDICATION_ABSTAIN_EVENT_TYPE
        assert t not in sentinel_routes._SENTINEL_FINDING_EVENT_TYPES
        assert t not in sentinel_routes._SENTINEL_ADJUDICATION_OUTCOME_TYPES
        # And it is not an outcome_type shape at all — the progress query keys
        # on '%_confirmed'/'%_dismissed'.
        assert not t.endswith(("_confirmed", "_dismissed"))

    def test_abstain_still_409s_on_an_already_adjudicated_fingerprint(self, client):
        """A real verdict outranks a later abstention; it must not be shadowed."""
        adjudicated, recorder = self._patches(already={"fpC"})
        with adjudicated, recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fpC", "status": "abstain"},
                            headers=_op_headers())
        assert r.status_code == 409

    def test_abstain_requires_the_same_operator_gate(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fpD", "status": "abstain"})
        assert r.status_code == 403

    def test_unknown_status_still_rejected(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fpE", "status": "maybe"},
                        headers=_op_headers())
        assert r.status_code == 400
        assert "abstain" in r.json()["error"]

    def test_abstain_needs_no_reason(self, client):
        """Declining must cost nothing — requiring a reason recreates the
        friction that made confirmation the cheapest exit."""
        adjudicated, recorder = self._patches()
        appended = AsyncMock(return_value=True)
        with adjudicated, recorder, \
                patch("src.db.get_db", return_value=type("DB", (), {"append_audit_event": appended})()):
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fpF", "status": "abstain"},
                            headers=_op_headers())
        assert r.status_code == 200
