"""Tests for Pydantic response models."""

import pytest

from unitares_sdk.errors import GovernanceError, IdentityDriftError, VerdictError
from unitares_sdk.models import (
    ArchiveResult,
    AuditResult,
    CheckinResult,
    CleanupResult,
    IdentityResult,
    InferenceHostResult,
    InferenceHostsResult,
    InferenceProvenance,
    ModelResult,
    OnboardResult,
    SearchResult,
)


# --- OnboardResult ---


def test_onboard_minimal():
    r = OnboardResult(success=True, client_session_id="sid-1")
    assert r.success is True
    assert r.client_session_id == "sid-1"
    assert r.uuid is None
    assert r.verdict == "proceed"


def test_onboard_full():
    r = OnboardResult(
        success=True,
        client_session_id="sid-1",
        uuid="u-123",
        continuity_token="v1.tok.sig",
        continuity_token_supported=True,
        is_new=True,
        verdict="proceed",
        session_resolution_source="explicit",
        welcome="Hello",
    )
    assert r.uuid == "u-123"
    assert r.continuity_token_supported is True


def test_onboard_extra_fields_ignored():
    """Server may return extra fields we don't model — they should not break parsing."""
    r = OnboardResult(
        success=True,
        client_session_id="sid-1",
        unknown_future_field="surprise",
        thread_context={"some": "data"},
    )
    assert r.success is True
    assert not hasattr(r, "unknown_future_field")


# --- CheckinResult ---


def test_checkin_with_metrics():
    r = CheckinResult(
        success=True,
        verdict="proceed",
        coherence=0.85,
        risk=0.1,
        metrics={"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0, "coherence": 0.85},
    )
    assert r.verdict == "proceed"
    assert r.metrics["E"] == 0.7


def test_checkin_guide_verdict():
    r = CheckinResult(
        success=True,
        verdict="guide",
        guidance="Entropy rising, reduce complexity",
        margin="tight",
    )
    assert r.verdict == "guide"
    assert r.margin == "tight"


# --- IdentityResult ---


def test_identity_result():
    r = IdentityResult(
        client_session_id="sid-1",
        uuid="u-123",
        continuity_token="v1.tok.sig",
        resolution_source="continuity_token",
    )
    assert r.uuid == "u-123"


def test_identity_result_accepts_session_resolution_source():
    r = IdentityResult(
        client_session_id="sid-1",
        uuid="u-123",
        session_resolution_source="continuity_token",
    )
    assert r.resolution_source == "continuity_token"


# --- SearchResult ---


def test_search_result_empty():
    r = SearchResult()
    assert r.success is True
    assert r.results == []


def test_search_result_with_items():
    r = SearchResult(
        results=[
            {"id": "d1", "summary": "Bug found", "tags": ["watcher"]},
            {"id": "d2", "summary": "Test pass", "tags": ["vigil"]},
        ]
    )
    assert len(r.results) == 2


def test_search_result_failure_preserved():
    r = SearchResult(success=False, error="boom")
    assert r.success is False
    assert r.error == "boom"


# --- ModelResult ---


def test_model_result():
    r = ModelResult(
        success=True,
        response="The code looks correct.",
        model_used="gemma4:latest",
        tokens_used=42,
        energy_cost=0.01,
        routed_via="ollama",
        task_type="analysis",
        inference={
            "schema": "unitares.inference_result.v0",
            "host_id": "ollama:local",
            "provider_kind": "ollama",
            "tokens_used": 42,
        },
    )
    assert r.response == "The code looks correct."
    assert r.model_used == "gemma4:latest"
    assert r.tokens_used == 42
    assert r.inference is not None
    assert r.inference.host_id == "ollama:local"
    assert r.inference.provider_kind == "ollama"


def test_inference_hosts_result():
    r = InferenceHostsResult.model_validate({
        "success": True,
        "schema": "unitares.inference_hosts.v0",
        "count": 1,
        "hosts": [{
            "host_id": "ollama:local",
            "display_name": "Ollama local",
            "provider_kind": "ollama",
            "configured": True,
            "available": True,
        }],
    })
    assert r.success is True
    assert r.schema_name == "unitares.inference_hosts.v0"
    assert r.count == 1
    assert r.hosts[0].host_id == "ollama:local"


def test_inference_host_result():
    r = InferenceHostResult.model_validate({
        "success": True,
        "schema": "unitares.inference_host.v0",
        "host": {
            "host_id": "hf:router",
            "provider_kind": "hf",
            "configured": True,
            "available": True,
        },
    })
    assert r.host is not None
    assert r.schema_name == "unitares.inference_host.v0"
    assert r.host.provider_kind == "hf"


def test_inference_provenance_defaults_warnings_to_list():
    r = InferenceProvenance(host_id="ollama:local")
    assert r.warnings == []


# --- Error hierarchy ---


def test_errors_inherit_from_base():
    assert issubclass(IdentityDriftError, GovernanceError)
    assert issubclass(VerdictError, GovernanceError)


def test_identity_drift_error_message():
    e = IdentityDriftError("uuid-aaaa", "uuid-bbbb")
    assert "uuid-aaaa" in str(e)
    assert "uuid-bbbb" in str(e)
    assert e.expected_uuid == "uuid-aaaa"
    assert e.received_uuid == "uuid-bbbb"


def test_verdict_error_message():
    e = VerdictError("pause", "Entropy too high")
    assert "pause" in str(e)
    assert "Entropy too high" in str(e)
    assert e.verdict == "pause"
    assert e.guidance == "Entropy too high"


def test_verdict_error_no_guidance():
    e = VerdictError("reject")
    assert "reject" in str(e)
    assert e.guidance is None


# --- AuditResult ---


def test_audit_parses_audit_payload():
    """Regression: server returns audit data under `audit`, not `results`.

    Vigil's groundskeeper silently failed for months because AuditResult
    only modeled `results: list[dict]` while the server sent `audit: dict`.
    The wire payload must hydrate into `audit` so callers can read
    buckets/top_stale without iterating an empty list.
    """
    # Shape observed from knowledge(action="audit") REST response:
    wire = {
        "success": True,
        "audit": {
            "timestamp": "2026-04-16T22:37:09.058725",
            "scope": "open",
            "total_audited": 184,
            "buckets": {
                "healthy": 49,
                "aging": 8,
                "stale": 0,
                "candidate_for_archive": 127,
            },
            "top_stale": [],
        },
    }
    r = AuditResult.model_validate(wire)
    assert r.success is True
    assert r.audit is not None
    assert r.audit["buckets"]["candidate_for_archive"] == 127
    # Vigil's groundskeeper parser relies on this exact access path:
    stale_found = (
        r.audit["buckets"].get("stale", 0)
        + r.audit["buckets"].get("candidate_for_archive", 0)
    )
    assert stale_found == 127


def test_audit_empty_on_no_payload():
    r = AuditResult(success=False, error="nope")
    assert r.audit is None
    assert r.error == "nope"


# --- CleanupResult / ArchiveResult wire-shape regressions ---


def test_cleanup_result_sums_from_server_wrapper():
    """Server returns counters under `cleanup_result`, not `cleaned`.

    Vigil's groundskeeper reported `0 archived` even after successful
    cleanups because the legacy `cleaned` field stayed at its default.
    `cleaned_total` sums the real counters.
    """
    wire = {
        "success": True,
        "cleanup_result": {
            "discoveries_archived": 12,
            "ephemeral_archived": 4,
            "discoveries_deleted": 0,
            "skipped_permanent": 3,
        },
    }
    r = CleanupResult.model_validate(wire)
    assert r.success is True
    assert r.cleaned == 0  # legacy field stays default
    assert r.cleaned_total == 16


def test_archive_result_reads_archived_count():
    """Server returns `archived_count`, not `archived`. Vigil now falls
    back to archived_count so orphan cleanups are counted correctly."""
    wire = {
        "success": True,
        "archived_count": 11,
        "dry_run": False,
    }
    r = ArchiveResult.model_validate(wire)
    assert r.archived_count == 11
    assert r.archived == 0  # legacy field
    # What Vigil reports:
    assert (r.archived_count or r.archived) == 11
