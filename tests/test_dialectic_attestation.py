"""
Regression tests for v2 bilateral attestation (NEW-2, council 2026-05-06).

Bug: handle_submit_synthesis computed signature_a and signature_b both over
the SAME last synthesis message (last_msg.sign(api_key_a),
last_msg.sign(api_key_b)). The reviewer's "signature" was over a message
they never wrote — only signed with their key. Bilateral cryptographic
attestation was effectively single-signer-with-two-keys.

Fix: Resolution.signature_version=2 attestation. finalize_resolution now
signs the canonical resolution payload (action, conditions, root_cause,
reasoning, timestamp — sorted/deterministic) with each agent's own api_key
independently. verify_signatures() can independently confirm that both
parties signed the same payload.
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import (
    DialecticSession,
    DialecticMessage,
    DialecticPhase,
    Resolution,
    ResolutionAction,
)


def _converged_session(api_key_a="key-a", api_key_b="key-b"):
    """SYNTHESIS-phase session with thesis + antithesis + agreed synthesis from each side."""
    s = DialecticSession(
        paused_agent_id="agent-a",
        reviewer_agent_id="agent-b",
        session_type="recovery",
    )
    s.phase = DialecticPhase.SYNTHESIS
    s.synthesis_round = 2
    now = datetime.now(timezone.utc).isoformat()
    s.transcript.append(DialecticMessage(
        phase="thesis", agent_id="agent-a", timestamp=now,
        root_cause="initial cause", proposed_conditions=["c1"],
        reasoning="initial",
    ))
    s.transcript.append(DialecticMessage(
        phase="antithesis", agent_id="agent-b", timestamp=now,
        reasoning="counter", concerns=["c"],
    ))
    s.transcript.append(DialecticMessage(
        phase="synthesis", agent_id="agent-a", timestamp=now,
        proposed_conditions=["agreed"], root_cause="agreed cause",
        reasoning="from a", agrees=True,
    ))
    s.transcript.append(DialecticMessage(
        phase="synthesis", agent_id="agent-b", timestamp=now,
        proposed_conditions=["agreed"], root_cause="agreed cause",
        reasoning="from b", agrees=True,
    ))
    s.phase = DialecticPhase.RESOLVED  # finalize_resolution requires this
    return s


class TestResolutionSchema:
    def test_legacy_default_signature_version_is_one(self):
        """Plain Resolution() construction lands at v1 (legacy schema) so
        existing on-disk records continue to round-trip."""
        r = Resolution(
            action="resume", conditions=["c"], root_cause="rc",
            reasoning="r", signature_a="x", signature_b="y",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        assert r.signature_version == 1

    def test_v1_resolution_does_not_verify(self):
        """v1 resolutions cannot be verified — the source message is not
        recoverable from the resolution row alone."""
        r = Resolution(
            action="resume", conditions=["c"], root_cause="rc",
            reasoning="r",
            signature_a="anything", signature_b="anything",
            timestamp=datetime.now(timezone.utc).isoformat(),
            signature_version=1,
        )
        assert r.verify_signatures("any-key", "any-key") is False

    def test_canonical_payload_is_deterministic(self):
        """Same content → same canonical payload, regardless of conditions
        list order. Lists must be sorted; sort_keys=True on the JSON dump."""
        ts = "2026-05-07T12:00:00+00:00"
        r1 = Resolution(
            action="resume", conditions=["zeta", "alpha", "mu"],
            root_cause="rc", reasoning="r",
            signature_a="", signature_b="", timestamp=ts,
            signature_version=2,
        )
        r2 = Resolution(
            action="resume", conditions=["alpha", "mu", "zeta"],
            root_cause="rc", reasoning="r",
            signature_a="", signature_b="", timestamp=ts,
            signature_version=2,
        )
        assert r1.canonical_payload() == r2.canonical_payload()

    def test_canonical_payload_excludes_signatures(self):
        """Changing the signatures must not change the canonical payload —
        otherwise sign-then-verify becomes a chicken-and-egg problem."""
        ts = "2026-05-07T12:00:00+00:00"
        r1 = Resolution(
            action="resume", conditions=["c"], root_cause="rc", reasoning="r",
            signature_a="A", signature_b="B", timestamp=ts, signature_version=2,
        )
        r2 = Resolution(
            action="resume", conditions=["c"], root_cause="rc", reasoning="r",
            signature_a="DIFFERENT", signature_b="DIFFERENT", timestamp=ts,
            signature_version=2,
        )
        assert r1.canonical_payload() == r2.canonical_payload()


class TestBilateralAttestation:
    def test_finalize_produces_v2_resolution(self):
        s = _converged_session()
        r = s.finalize_resolution(api_key_a="key-a", api_key_b="key-b")
        assert r.signature_version == 2

    def test_finalize_signatures_verify_with_correct_keys(self):
        s = _converged_session()
        r = s.finalize_resolution(api_key_a="key-a", api_key_b="key-b")
        assert r.verify_signatures("key-a", "key-b") is True

    def test_finalize_signatures_do_not_verify_with_wrong_keys(self):
        s = _converged_session()
        r = s.finalize_resolution(api_key_a="key-a", api_key_b="key-b")
        assert r.verify_signatures("key-a", "wrong-key") is False
        assert r.verify_signatures("wrong-key", "key-b") is False

    def test_signatures_are_distinct_per_agent(self):
        """The two signatures must NOT be equal — that was the NEW-2 bug
        (both over the same payload + same key shape produced identical
        hashes when both parties used the same api_key, and over the same
        last_msg with different keys produced same-message-different-key
        which isn't bilateral attestation)."""
        s = _converged_session()
        r = s.finalize_resolution(api_key_a="key-a", api_key_b="key-b")
        assert r.signature_a != r.signature_b
        assert r.signature_a != ""
        assert r.signature_b != ""

    def test_swapping_keys_at_verify_time_fails(self):
        """signature_a is signed by api_key_a; supplying api_key_b in its
        place must fail. Otherwise swap-attacks succeed silently."""
        s = _converged_session()
        r = s.finalize_resolution(api_key_a="key-a", api_key_b="key-b")
        assert r.verify_signatures("key-b", "key-a") is False  # swapped

    def test_tampered_resolution_fails_verification(self):
        """Mutating any canonical-payload field after signing must
        invalidate the signatures."""
        s = _converged_session()
        r = s.finalize_resolution(api_key_a="key-a", api_key_b="key-b")
        assert r.verify_signatures("key-a", "key-b") is True

        # Tamper with conditions
        r.conditions.append("attacker-injected")
        assert r.verify_signatures("key-a", "key-b") is False

    def test_empty_reviewer_key_falls_back_to_unverifiable(self):
        """LLM-assisted dialectic passes empty api_key_b — verify must
        return False (not vacuously True from empty==empty)."""
        s = _converged_session()
        r = s.finalize_resolution(api_key_a="key-a", api_key_b="")
        assert r.signature_b == ""
        assert r.verify_signatures("key-a", "") is False
        assert r.verify_signatures("key-a", "key-b") is False  # any non-empty key fails too

    def test_v2_resolution_serializes_signature_version(self):
        s = _converged_session()
        r = s.finalize_resolution(api_key_a="key-a", api_key_b="key-b")
        d = r.to_dict()
        assert d["signature_version"] == 2
        # Round-trip preserves everything
        roundtrip = Resolution(**d)
        assert roundtrip.verify_signatures("key-a", "key-b") is True


class TestNew2RegressionGuard:
    """Direct guards that the specific NEW-2 failure mode cannot recur."""

    def test_signatures_are_not_simply_last_message_hashes(self):
        """If finalize_resolution regressed to signing the last synthesis
        message (the NEW-2 bug shape), signature_a would equal
        last_msg.sign(api_key_a). Assert it does NOT — the v2 signatures
        are over the canonical resolution payload."""
        s = _converged_session()
        # Capture last agreed synthesis message before finalization
        last_msg = next(
            m for m in reversed(s.transcript)
            if m.phase == "synthesis" and m.agrees
        )
        legacy_a = last_msg.sign("key-a")
        r = s.finalize_resolution(api_key_a="key-a", api_key_b="key-b")
        assert r.signature_a != legacy_a, (
            "v2 attestation must sign the canonical resolution payload, "
            "NOT the last synthesis message (NEW-2 regression)"
        )

    def test_two_resolutions_with_same_keys_produce_same_signatures_only_when_payload_matches(self):
        """Determinism property: two identical canonical payloads signed
        with the same keys produce the same signatures. Two different
        payloads do not."""
        ts = "2026-05-07T12:00:00+00:00"
        r1 = Resolution(
            action="resume", conditions=["c"], root_cause="rc", reasoning="r",
            signature_a="", signature_b="", timestamp=ts, signature_version=2,
        )
        r2 = Resolution(
            action="resume", conditions=["c"], root_cause="rc", reasoning="r",
            signature_a="", signature_b="", timestamp=ts, signature_version=2,
        )
        r3 = Resolution(
            action="resume", conditions=["DIFFERENT"], root_cause="rc",
            reasoning="r", signature_a="", signature_b="", timestamp=ts,
            signature_version=2,
        )
        sig_r1 = Resolution.compute_signature(r1.canonical_payload(), "k")
        sig_r2 = Resolution.compute_signature(r2.canonical_payload(), "k")
        sig_r3 = Resolution.compute_signature(r3.canonical_payload(), "k")
        assert sig_r1 == sig_r2
        assert sig_r1 != sig_r3
