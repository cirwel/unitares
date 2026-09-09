"""The read path must SAY what party attestation a resolution actually carries.

Companion to test_dialectic_attestation.py, which pins the v2 signing scheme
itself. This file is about how that scheme is presented to a reader.

``signature_version`` is a SCHEME label, not a claim that anyone signed, and
``compute_signature`` returns an empty string when an api_key is absent. So the
shape ``{"signature_version": 2, "signature_a": "", "signature_b": ""}`` reads
as a v2 attestation until a reader notices both signatures are empty strings.

Measured on the live corpus 2026-09-08: no agent has been minted with an
api_key since 2026-01-29, so that unsigned-but-stamped-v2 shape is what every
resolution since 2026-06-23 looks like. ``describe_attestation`` states the
condition instead of leaving it to be inferred; it does not change what is
stored, signed, or hashed, and these tests pin both halves of that.
"""

from __future__ import annotations

import pytest

from src.dialectic_protocol import (
    ATTESTATION_BILATERAL,
    ATTESTATION_LEGACY_V1,
    ATTESTATION_SINGLE_SIGNER,
    ATTESTATION_UNSIGNED,
    Resolution,
    describe_attestation,
)

SIG_A = "a" * 64
SIG_B = "b" * 64


def _resolution(**overrides) -> Resolution:
    base = dict(
        action="resume",
        conditions=["hold complexity at 0.3"],
        root_cause="agreed",
        reasoning="agreed",
        signature_a="",
        signature_b="",
        timestamp="2026-09-08T00:00:00+00:00",
        signature_version=2,
    )
    base.update(overrides)
    return Resolution(**base)


class TestStates:
    def test_no_resolution_describes_nothing(self):
        assert describe_attestation(None) is None

    def test_the_live_corpus_shape_is_unsigned_not_v2_attested(self):
        """The whole point: v2 stamped, nobody signed."""
        described = describe_attestation(
            {"signature_version": 2, "signature_a": "", "signature_b": ""}
        )
        assert described["state"] == ATTESTATION_UNSIGNED
        assert described["signer_count"] == 0
        assert described["signature_version"] == 2

    def test_single_signer_is_not_reported_as_bilateral(self):
        """The LLM-assisted path signs A only, by design."""
        described = describe_attestation(
            {"signature_version": 2, "signature_a": SIG_A, "signature_b": ""}
        )
        assert described["state"] == ATTESTATION_SINGLE_SIGNER
        assert described["signer_count"] == 1

    def test_bilateral(self):
        described = describe_attestation(
            {"signature_version": 2, "signature_a": SIG_A, "signature_b": SIG_B}
        )
        assert described["state"] == ATTESTATION_BILATERAL
        assert described["signer_count"] == 2

    def test_legacy_v1_is_named_rather_than_called_bilateral(self):
        described = describe_attestation(
            {"signature_version": 1, "signature_a": SIG_A, "signature_b": SIG_B}
        )
        assert described["state"] == ATTESTATION_LEGACY_V1

    def test_v1_with_no_signatures_is_unsigned_not_legacy(self):
        described = describe_attestation(
            {"signature_version": 1, "signature_a": "", "signature_b": ""}
        )
        assert described["state"] == ATTESTATION_UNSIGNED

    def test_missing_version_defaults_to_legacy_decode(self):
        """Absent version means an old on-disk row, matching Resolution's default."""
        assert (
            describe_attestation({"signature_a": SIG_A, "signature_b": SIG_B})["state"]
            == ATTESTATION_LEGACY_V1
        )

    @pytest.mark.parametrize("bad", ["", None, "two", [], {}])
    def test_an_unparseable_version_degrades_to_legacy_never_raises(self, bad):
        described = describe_attestation(
            {"signature_version": bad, "signature_a": SIG_A, "signature_b": SIG_B}
        )
        assert described["signature_version"] == 1
        assert described["state"] == ATTESTATION_LEGACY_V1

    @pytest.mark.parametrize("value", [0, 1, 2, "2", None, "", "two"])
    def test_version_coercion_matches_the_reconstruction_path_exactly(self, value):
        """Two copies of this coercion disagreed on a stored 0 before review.

        The read path rebuilds a Resolution with _coerce_signature_version; the
        descriptor reports one. If they diverge, the same row is described as
        legacy and reconstructed as something else.
        """
        from src.mcp_handlers.dialectic.session import _coerce_signature_version

        described = describe_attestation(
            {"signature_version": value, "signature_a": "", "signature_b": ""}
        )
        assert described["signature_version"] == _coerce_signature_version(value)

    def test_the_descriptor_carries_no_prose(self):
        """list() pages 50 sessions; a constant per-row string re-inflates it."""
        described = describe_attestation(
            {"signature_version": 2, "signature_a": "", "signature_b": ""}
        )
        assert set(described) == {"state", "signature_version", "signer_count"}
        assert all(not isinstance(v, str) or len(v) < 40 for v in described.values())

    def test_accepts_a_resolution_object_as_well_as_a_dict(self):
        obj = _resolution(signature_a=SIG_A, signature_b=SIG_B)
        assert (
            describe_attestation(obj)["state"]
            == describe_attestation(obj.to_dict())["state"]
            == ATTESTATION_BILATERAL
        )


class TestDescriptorIsDerivedNeverStored:
    """Adding a stored field would move resolution_hash and the receipt digest.

    ``Resolution.hash()`` is served as ``resolution_hash``, and the drr.v1
    receipt is minted over the record's listed fields, so the descriptor has to
    stay on the read path. These pin that it did.
    """

    def test_to_dict_gains_no_attestation_key(self):
        assert "attestation" not in _resolution().to_dict()
        assert "state" not in _resolution().to_dict()

    def test_hash_is_unaffected_by_describing_the_resolution(self):
        resolution = _resolution(signature_a=SIG_A, signature_b=SIG_B)
        before = resolution.hash()
        describe_attestation(resolution)
        assert resolution.hash() == before

    def test_canonical_payload_is_unaffected(self):
        resolution = _resolution(signature_a=SIG_A, signature_b=SIG_B)
        before = resolution.canonical_payload()
        describe_attestation(resolution)
        assert resolution.canonical_payload() == before

    def test_describing_does_not_mutate_the_input_dict(self):
        record = {"signature_version": 2, "signature_a": "", "signature_b": ""}
        describe_attestation(record)
        assert record == {
            "signature_version": 2,
            "signature_a": "",
            "signature_b": "",
        }


class TestAgreesWithTheVerifier:
    """The descriptor must never claim more than verify_signatures() allows."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"signature_a": "", "signature_b": ""},
            {"signature_a": SIG_A, "signature_b": ""},
            {"signature_a": "", "signature_b": SIG_B},
        ],
    )
    def test_anything_short_of_two_signatures_fails_verification(self, kwargs):
        resolution = _resolution(**kwargs)
        assert describe_attestation(resolution)["state"] != ATTESTATION_BILATERAL
        assert resolution.verify_signatures("key-a", "key-b") is False

    def test_a_genuinely_bilateral_row_describes_and_verifies(self):
        """End to end with real keys, which is the case that has gone extinct."""
        proto = _resolution()
        payload = proto.canonical_payload()
        signed = _resolution(
            signature_a=Resolution.compute_signature(payload, "key-a"),
            signature_b=Resolution.compute_signature(payload, "key-b"),
        )
        assert describe_attestation(signed)["state"] == ATTESTATION_BILATERAL
        assert signed.verify_signatures("key-a", "key-b") is True

    def test_empty_keys_produce_the_unsigned_state_the_fleet_actually_has(self):
        """No api_key anywhere means no signature anywhere. That is the finding."""
        proto = _resolution()
        payload = proto.canonical_payload()
        unsigned = _resolution(
            signature_a=Resolution.compute_signature(payload, ""),
            signature_b=Resolution.compute_signature(payload, ""),
        )
        assert describe_attestation(unsigned)["state"] == ATTESTATION_UNSIGNED
        assert unsigned.verify_signatures("", "") is False


class TestAttachAttestation:
    """One helper, used at every response site that carries a resolution.

    Codex and the code review both found the first draft wiring only the
    PostgreSQL fast path, so whether a caller saw `attestation` depended on
    check_timeout, on whether they looked the session up by agent_id, and on
    whether they were reading the synthesis response that mints the resolution
    in the first place. A field that exists to stop a reader inferring a state
    is worse than useless if its own presence has to be inferred.
    """

    def test_adds_the_descriptor_when_a_resolution_is_present(self):
        from src.mcp_handlers.dialectic.session import attach_attestation

        payload = attach_attestation(
            {
                "session_id": "s1",
                "resolution": {
                    "signature_version": 2,
                    "signature_a": "",
                    "signature_b": "",
                },
            }
        )
        assert payload["attestation"]["state"] == ATTESTATION_UNSIGNED

    def test_absent_rather_than_null_when_there_is_no_resolution(self):
        from src.mcp_handlers.dialectic.session import attach_attestation

        assert "attestation" not in attach_attestation({"session_id": "s1"})
        assert "attestation" not in attach_attestation(
            {"session_id": "s1", "resolution": None}
        )

    def test_leaves_the_stored_resolution_object_untouched(self):
        from src.mcp_handlers.dialectic.session import attach_attestation

        resolution = {
            "signature_version": 2,
            "signature_a": "",
            "signature_b": "",
        }
        attach_attestation({"resolution": resolution})
        assert resolution == {
            "signature_version": 2,
            "signature_a": "",
            "signature_b": "",
        }

    def test_non_dict_payloads_pass_through(self):
        from src.mcp_handlers.dialectic.session import attach_attestation

        assert attach_attestation(None) is None
        assert attach_attestation([1, 2]) == [1, 2]

    def test_every_handler_site_that_serves_a_resolution_uses_the_helper(self):
        """Pins the surface-consistency fix so a new response site cannot skip it."""
        import re
        from pathlib import Path

        handlers = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "mcp_handlers"
            / "dialectic"
            / "handlers.py"
        )
        lines = handlers.read_text().split("\n")
        serving = [
            i
            for i, ln in enumerate(lines)
            if re.fullmatch(r'\s*result\["resolution"\] = resolution\.to_dict\(\)', ln)
        ]
        assert serving, "expected handlers.py to still build resolution responses"
        for i in serving:
            assert "attach_attestation(result)" in lines[i + 1], (
                f"handlers.py:{i + 1} serves a resolution without the descriptor"
            )


class TestNoForgeableFallbackKey:
    """A signature derived from public data is worse than no signature.

    Until 2026-09-09 the LLM-assisted finalize path, when no api_key was on
    file, signed with a key derived as f"llm-{agent_uuid[:8]}". The uuid is
    served publicly in session reads, so anyone able to see the session could
    recompute that "signature" — it attested nothing while reading as attested.
    It produced every signature_a written in 2026 (4 rows).

    With no key, compute_signature returns "" and describe_attestation reports
    the record as `unsigned`, which is the truth.
    """

    def test_an_empty_key_yields_no_signature_and_an_unsigned_verdict(self):
        proto = _resolution()
        payload = proto.canonical_payload()
        assert Resolution.compute_signature(payload, "") == ""
        unsigned = _resolution(
            signature_a=Resolution.compute_signature(payload, ""),
            signature_b="",
        )
        assert describe_attestation(unsigned)["state"] == ATTESTATION_UNSIGNED

    def test_a_uuid_derived_key_would_have_read_as_attested(self):
        """Shows what the old fallback bought: single_signer, not unsigned.

        This is the whole harm — the record claimed a party attested when the
        'secret' was recomputable from a public identifier.
        """
        proto = _resolution()
        payload = proto.canonical_payload()
        forged = _resolution(
            signature_a=Resolution.compute_signature(payload, "llm-d81d5ab6"),
            signature_b="",
        )
        assert describe_attestation(forged)["state"] == ATTESTATION_SINGLE_SIGNER

    def test_no_finalize_site_derives_a_key_from_an_agent_uuid(self):
        """Pins the removal so it cannot creep back in."""
        import pathlib
        import re

        handlers = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src" / "mcp_handlers" / "dialectic" / "handlers.py"
        )
        for i, line in enumerate(handlers.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            assert not re.search(r'f"llm-\{agent_uuid', line), (
                f"handlers.py:{i} re-derives a signing key from a public uuid"
            )
