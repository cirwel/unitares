import json

from src.mcp_handlers.context import SessionSignals
from src.model_harness_provenance import (
    RUNTIME_PROVENANCE_SCHEMA,
    build_runtime_provenance,
    exact_model_attribution_status,
    infer_harness_version,
    normalize_persisted_runtime_provenance,
    runtime_signal_fields_from_headers,
)


def test_transport_reported_exact_model_is_eligible_with_complete_harness():
    signals = SessionSignals(
        reported_model="gpt-5.6-sol",
        model_provider="openai",
        model_provenance_source="provider_reported",
        reported_harness_type="codex-cli",
        harness_version="0.115.0",
        harness_provenance_source="harness_reported",
        transport="mcp",
    )

    envelope = build_runtime_provenance({}, signals=signals)

    assert envelope["schema"] == RUNTIME_PROVENANCE_SCHEMA
    assert envelope["model"] == {
        "identifier": "gpt-5.6-sol",
        "provider": "openai",
        "source": "provider_reported",
        "provider_source": "provider_reported",
        "reporting_channel": "transport_header",
        "exact": True,
        "verification": "provider_reported_unverified",
        "missing_reason": None,
        "provider_missing_reason": None,
    }
    assert envelope["harness"]["type"] == "codex-cli"
    assert envelope["harness"]["version"] == "0.115.0"
    assert exact_model_attribution_status(envelope) == "eligible_exact"


def test_legacy_flat_model_is_visible_but_never_promoted_to_exact():
    envelope = build_runtime_provenance(
        {
            "model_type": "gpt-5.6-sol",
            "client_hint": "codex-cli",
            "harness_version": "0.115.0",
        }
    )

    assert envelope["model"]["identifier"] == "gpt-5.6-sol"
    assert envelope["model"]["source"] == "caller_declared"
    assert envelope["model"]["exact"] is False
    assert exact_model_attribution_status(envelope) == "model_unverified"


def test_user_agent_fallback_is_family_only_and_records_harness_version():
    signals = SessionSignals(
        user_agent="codex_cli_rs/0.115.0 (macOS)",
        client_hint="codex-cli",
        transport="mcp",
    )

    envelope = build_runtime_provenance({}, signals=signals)

    assert envelope["model"]["identifier"] == "gpt-family"
    assert envelope["model"]["source"] == "transport_inferred"
    assert envelope["model"]["exact"] is False
    assert envelope["harness"]["version"] == "0.115.0"
    assert envelope["harness"]["version_source"] == "transport_user_agent"
    assert exact_model_attribution_status(envelope) == "inferred_family_only"


def test_unavailable_values_are_explicit():
    envelope = build_runtime_provenance({})

    assert envelope["model"]["identifier"] is None
    assert envelope["model"]["source"] == "unavailable"
    assert envelope["model"]["missing_reason"] == "not_exposed"
    assert envelope["harness"]["type"] is None
    assert envelope["harness"]["version_missing_reason"] == "not_exposed"
    assert envelope["authority"] == {
        "role": "descriptive_context",
        "is_identity_proof": False,
        "is_verdict_authority": False,
        "is_policy_dispatch_key": False,
    }


def test_sensitive_and_oversized_values_are_rejected_not_truncated():
    secret = "https://operator:password@example.test/model?token=abc123"
    envelope = build_runtime_provenance(
        {
            "provenance_context": {
                "runtime_provenance": {
                    "model": {
                        "identifier": secret,
                        "source": "provider_reported",
                        "exact": True,
                    },
                    "harness": {
                        "type": "codex-cli",
                        "version": "v" * 200,
                        "type_source": "harness_reported",
                        "version_source": "harness_reported",
                    },
                }
            }
        }
    )

    serialized = json.dumps(envelope)
    assert secret not in serialized
    assert "password" not in serialized
    assert envelope["model"]["missing_reason"] == "redacted_sensitive_value"
    assert envelope["harness"]["version"] is None
    assert envelope["harness"]["version_missing_reason"] == "value_too_long"
    assert exact_model_attribution_status(envelope) == "model_unavailable"


def test_persisted_legacy_or_unknown_schema_is_never_reinterpreted():
    legacy = normalize_persisted_runtime_provenance(
        {"model": {"identifier": "gpt-5.6-sol", "exact": True}}
    )
    future = normalize_persisted_runtime_provenance(
        {"schema": "s22.runtime_provenance.v2", "model": {"identifier": "x"}}
    )

    assert legacy["record_status"] == "legacy_unversioned"
    assert legacy["model"]["identifier"] is None
    assert future["record_status"] == "unsupported_schema"
    assert future["model"]["identifier"] is None


def test_header_contract_keeps_model_and_harness_sources_separate():
    fields = runtime_signal_fields_from_headers(
        {
            "X-Unitares-Model": "claude-opus-4-1-20250805",
            "X-Unitares-Model-Provider": "anthropic",
            "X-Unitares-Model-Source": "provider_reported",
            "X-Unitares-Harness-Type": "claude-code",
            "X-Unitares-Harness-Version": "1.0.83",
            "X-Unitares-Adapter-Type": "unitares-governance-plugin",
            "X-Unitares-Adapter-Version": "0.4.15",
        }
    )

    assert fields["reported_model"] == "claude-opus-4-1-20250805"
    assert fields["model_provenance_source"] == "provider_reported"
    assert fields["reported_harness_type"] == "claude-code"
    assert fields["harness_provenance_source"] == "harness_reported"
    assert fields["adapter_version"] == "0.4.15"


def test_header_contract_ignores_unscoped_lookalike_headers():
    fields = runtime_signal_fields_from_headers(
        {
            "X-Model": "not-an-adapter-contract",
            "X-Model-Provider": "not-a-provider-contract",
            "X-Harness-Type": "not-a-harness-contract",
            "X-Harness-Version": "9.9.9",
        }
    )

    assert fields["reported_model"] is None
    assert fields["model_provider"] is None
    assert fields["reported_harness_type"] is None
    assert fields["harness_version"] is None


def test_untrusted_reporting_channel_is_not_persisted():
    secret_channel = "Bearer " + "A" * 50
    envelope = build_runtime_provenance(
        {
            "provenance_context": {
                "runtime_provenance": {
                    "model": {
                        "identifier": "gpt-5.6-sol",
                        "source": "harness_reported",
                        "exact": True,
                        "reporting_channel": secret_channel,
                    },
                    "harness": {
                        "type": "codex-cli",
                        "type_source": "harness_reported",
                    },
                }
            }
        }
    )

    assert secret_channel not in json.dumps(envelope)
    assert envelope["model"]["reporting_channel"] == "request_context"


def test_exact_cohort_requires_observed_harness_source():
    caller_harness = build_runtime_provenance(
        {
            "provenance_context": {
                "runtime_provenance": {
                    "model": {
                        "identifier": "gpt-5.6-sol",
                        "source": "provider_reported",
                        "exact": True,
                    },
                    "harness": {
                        "type": "codex-cli",
                        "type_source": "caller_declared",
                    },
                }
            }
        }
    )
    caller_version = build_runtime_provenance(
        {
            "provenance_context": {
                "runtime_provenance": {
                    "model": {
                        "identifier": "gpt-5.6-sol",
                        "source": "provider_reported",
                        "exact": True,
                    },
                    "harness": {
                        "type": "codex-cli",
                        "type_source": "harness_reported",
                        "version": "0.115.0",
                        "version_source": "caller_declared",
                    },
                }
            }
        }
    )

    assert exact_model_attribution_status(caller_harness) == "harness_unverified"
    assert (
        exact_model_attribution_status(caller_version)
        == "harness_version_unverified"
    )


def test_harness_version_parser_refuses_generic_http_library_version():
    assert infer_harness_version("python-httpx/0.28.1", "codex-cli") is None
