"""Pure contract tests for the shadow-only advisory decision core."""

from __future__ import annotations

import math

import pytest

from src.advisory_alerts import (
    ADVISORY_MODE_ENV,
    AdvisoryMode,
    DegradationMetadata,
    DegradationReason,
    DeliveryReason,
    DeliveryStatus,
    EligibilityStatus,
    SuppressionMetadata,
    SuppressionReason,
    decide_advisory,
    resolve_advisory_mode,
)


def _inputs() -> dict:
    return {
        "candidate_type": "review_nudge",
        "subject": {
            "agent_id": "agent-1",
            "session_id": "session-1",
            "update_index": 7,
        },
        "measurement": {
            "confidence": 0.2,
            "source": "agent_report",
        },
        "policy": {
            "policy_id": "review-nudge-low-confidence",
            "policy_version": "v1",
            "operator": "lte",
            "threshold": 0.4,
        },
        "provenance": {
            "measurement_source": "process_agent_update",
            "code_revision": "deadbeef",
        },
    }


def test_mode_set_and_delivery_state_are_shadow_only():
    assert tuple(AdvisoryMode) == (AdvisoryMode.OFF, AdvisoryMode.SHADOW)
    assert tuple(DeliveryStatus) == (DeliveryStatus.NOT_DELIVERED,)


def test_master_mode_defaults_off_and_explicit_shadow_is_available():
    assert resolve_advisory_mode({}).mode is AdvisoryMode.OFF
    resolution = resolve_advisory_mode({ADVISORY_MODE_ENV: " SHADOW "})
    assert resolution.mode is AdvisoryMode.SHADOW
    assert resolution.degradation.degraded is False


@pytest.mark.parametrize("raw", ["surface", "live", "unexpected"])
def test_unknown_or_live_mode_fails_closed_with_typed_degradation(raw):
    resolution = resolve_advisory_mode({ADVISORY_MODE_ENV: raw})
    assert resolution.mode is AdvisoryMode.OFF
    assert resolution.degradation == DegradationMetadata.active(
        DegradationReason.INVALID_MODE,
        retryable=False,
        detail=f"unsupported {ADVISORY_MODE_ENV} value",
    )


def test_off_is_a_real_bypass_before_candidate_validation_or_hashing():
    decision = decide_advisory(
        candidate_type="",
        subject={"unsupported": object()},
        measurement={"not_finite": math.nan},
        policy={},
        eligible=True,
        environ={},
    )

    assert decision.mode is AdvisoryMode.OFF
    assert decision.eligibility is EligibilityStatus.NOT_EVALUATED
    assert decision.replay_id is None
    assert decision.candidate is None
    assert decision.suppression.reason is SuppressionReason.MASTER_BYPASS
    assert decision.delivery.status is DeliveryStatus.NOT_DELIVERED
    assert decision.delivery.reason is DeliveryReason.MODE_OFF


def test_shadow_candidate_and_replay_ids_are_deterministic_across_key_order():
    inputs = _inputs()
    first = decide_advisory(
        **inputs,
        eligible=True,
        environ={ADVISORY_MODE_ENV: "shadow"},
    )
    reordered = decide_advisory(
        candidate_type=inputs["candidate_type"],
        subject=dict(reversed(list(inputs["subject"].items()))),
        measurement=dict(reversed(list(inputs["measurement"].items()))),
        policy=dict(reversed(list(inputs["policy"].items()))),
        provenance=dict(reversed(list(inputs["provenance"].items()))),
        eligible=True,
        environ={ADVISORY_MODE_ENV: "shadow"},
    )

    assert first.replay_id == reordered.replay_id
    assert first.candidate_id == reordered.candidate_id
    assert first.replay_id.startswith("ar_")
    assert first.candidate_id.startswith("ac_")
    assert first.eligibility is EligibilityStatus.ELIGIBLE
    assert first.suppression.suppressed is False
    assert first.degradation.degraded is False
    assert first.delivery == reordered.delivery
    assert first.delivery.reason is DeliveryReason.SHADOW_ONLY


def test_replay_and_candidate_ids_change_when_measurement_changes():
    inputs = _inputs()
    first = decide_advisory(
        **inputs,
        eligible=True,
        environ={ADVISORY_MODE_ENV: "shadow"},
    )
    inputs["measurement"] = {**inputs["measurement"], "confidence": 0.21}
    changed = decide_advisory(
        **inputs,
        eligible=True,
        environ={ADVISORY_MODE_ENV: "shadow"},
    )

    assert first.replay_id != changed.replay_id
    assert first.candidate_id != changed.candidate_id


def test_not_eligible_is_replayable_but_does_not_create_a_candidate():
    eligible = decide_advisory(
        **_inputs(),
        eligible=True,
        environ={ADVISORY_MODE_ENV: "shadow"},
    )
    decision = decide_advisory(
        **_inputs(),
        eligible=False,
        environ={ADVISORY_MODE_ENV: "shadow"},
    )

    assert decision.replay_id is not None
    assert decision.replay_id != eligible.replay_id
    assert decision.candidate is None
    assert decision.candidate_id is None
    assert decision.eligibility is EligibilityStatus.NOT_ELIGIBLE
    assert decision.suppression.suppressed is False
    assert decision.delivery.reason is DeliveryReason.NOT_ELIGIBLE


def test_suppressed_candidate_remains_countable_and_never_delivered():
    suppression = SuppressionMetadata.active(
        SuppressionReason.NOVELTY_DEDUPLICATED,
        detail="same candidate already observed in this session",
    )
    decision = decide_advisory(
        **_inputs(),
        eligible=True,
        suppression=suppression,
        environ={ADVISORY_MODE_ENV: "shadow"},
    )

    assert decision.candidate is not None
    assert decision.suppression == suppression
    assert decision.delivery.status is DeliveryStatus.NOT_DELIVERED
    assert decision.delivery.reason is DeliveryReason.SUPPRESSED


def test_degraded_evaluation_is_typed_replayable_and_has_no_candidate():
    degradation = DegradationMetadata.active(
        DegradationReason.CAPABILITY_UNAVAILABLE,
        retryable=True,
        detail="shared memory unavailable",
    )
    decision = decide_advisory(
        **_inputs(),
        eligible=None,
        degradation=degradation,
        environ={ADVISORY_MODE_ENV: "shadow"},
    )

    assert decision.replay_id is not None
    assert decision.candidate is None
    assert decision.eligibility is EligibilityStatus.NOT_EVALUATED
    assert decision.degradation == degradation
    assert decision.delivery.reason is DeliveryReason.DEGRADED


def test_shadow_requires_an_explicit_eligibility_result_when_not_degraded():
    with pytest.raises(TypeError, match="eligible must be a bool"):
        decide_advisory(
            **_inputs(),
            eligible=None,
            environ={ADVISORY_MODE_ENV: "shadow"},
        )


def test_wire_record_uses_string_discriminants_and_preserves_provenance():
    decision = decide_advisory(
        **_inputs(),
        eligible=True,
        suppression=SuppressionMetadata.active(
            SuppressionReason.ATTENTION_BUDGET_EXHAUSTED
        ),
        environ={ADVISORY_MODE_ENV: "shadow"},
    )

    record = decision.to_dict()
    assert record["mode"] == "shadow"
    assert record["eligibility"] == "eligible"
    assert record["delivery"] == {
        "status": "not_delivered",
        "reason": "suppressed",
    }
    assert record["suppression"]["reason"] == "attention_budget_exhausted"
    assert record["candidate"]["provenance"]["code_revision"] == "deadbeef"
    assert record["candidate_id"] == record["candidate"]["candidate_id"]


def test_non_finite_measurement_is_rejected_in_shadow_mode():
    inputs = _inputs()
    inputs["measurement"] = {"confidence": math.nan}
    with pytest.raises(ValueError, match="non-finite float"):
        decide_advisory(
            **inputs,
            eligible=True,
            environ={ADVISORY_MODE_ENV: "shadow"},
        )


def test_active_suppression_and_degradation_cannot_be_conflated():
    with pytest.raises(ValueError, match="both suppressed and degraded"):
        decide_advisory(
            **_inputs(),
            eligible=None,
            suppression=SuppressionMetadata.active(
                SuppressionReason.OPERATOR_SUPPRESSED
            ),
            degradation=DegradationMetadata.active(
                DegradationReason.TIMEOUT,
                retryable=True,
            ),
            environ={ADVISORY_MODE_ENV: "shadow"},
        )
