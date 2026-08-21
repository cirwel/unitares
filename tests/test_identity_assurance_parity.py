"""Read-path / write-path identity-assurance parity (#1768).

The assurance tier is computed in two deliberately mirrored modules:
``src/services/identity_payloads.py`` (identity/onboard responses) and
``src/mcp_handlers/updates/phases.py`` (process_agent_update). Their source
sets drifted, so the same binding earned two different tiers: identity PATH 0's
proof-owned UUID rebind read strong from ``identity()`` but computed weak on
the very write the ``require_strong_identity`` hint told the caller it would
satisfy, and an operator-token binding was strong on writes while the read
path reported it weak with a wrong how_to_strengthen breadcrumb.

These tests pin the mirror as a contract: the sets must be identical, the
sources each module was missing must grade strong on both paths, and the
proof-origin anti-laundering downgrade (#679) must still beat every label.
"""

from src.mcp_handlers.updates import phases as write_path
from src.services import identity_payloads as read_path


def test_strong_and_medium_source_sets_are_identical():
    """The two modules claim to mirror each other; make drift a test failure
    instead of a tier disagreement discovered in production (#1768)."""
    assert write_path._STRONG_IDENTITY_SOURCES == read_path._STRONG_IDENTITY_SOURCES
    assert write_path._MEDIUM_IDENTITY_SOURCES == read_path._MEDIUM_IDENTITY_SOURCES


def test_every_source_grades_the_same_tier_on_both_paths():
    """Beyond set equality: for every known source label (plus an unknown
    one), the computed tier must agree between the read and write paths."""
    sources = (
        write_path._STRONG_IDENTITY_SOURCES
        | write_path._MEDIUM_IDENTITY_SOURCES
        | {"ip_ua_fingerprint", "totally_unknown_source"}
    )
    for source in sources:
        write_tier = write_path._compute_identity_assurance(source, None)["tier"]
        read_tier = read_path._identity_assurance_from_source(source)["tier"]
        assert write_tier == read_tier, (
            f"source {source!r}: write path says {write_tier}, "
            f"read path says {read_tier}"
        )


def test_proof_owned_uuid_rebind_is_strong_on_the_write_path():
    """PATH 0 (`agent_uuid_direct*`) requires ownership proof — a signed
    continuity_token whose `aid` matches the UUID, or an S19 kernel-attested
    substrate peer. The require_strong_identity refusal recommends exactly
    this rebind in its hint, so it must actually satisfy the gate."""
    for source in ("agent_uuid_direct", "agent_uuid_direct_fastpath"):
        assurance = write_path._compute_identity_assurance(
            source, None, proof_origin="caller_asserted"
        )
        assert assurance["tier"] == "strong"
        assert assurance["caller_proven"] is True


def test_operator_token_is_strong_on_the_read_path():
    """The operator bearer token is validated against the env allowlist on
    every call (#425) — per-call proof. The read path must not report it weak
    while the write path scores the same binding strong."""
    assurance = read_path._identity_assurance_from_source(
        "operator_token", proof_origin="caller_asserted"
    )
    assert assurance["tier"] == "strong"
    assert "how_to_strengthen" not in assurance


def test_server_inferred_still_downgrades_the_new_strong_sources():
    """The #679 anti-laundering rule is authoritative over every label: a
    server-inferred binding wearing any of the newly aligned strong labels is
    still weak on both paths."""
    for source in ("agent_uuid_direct", "operator_token", "client_session_id"):
        write_assurance = write_path._compute_identity_assurance(
            source, None, proof_origin="server_inferred"
        )
        read_assurance = read_path._identity_assurance_from_source(
            source, proof_origin="server_inferred"
        )
        assert write_assurance["tier"] == "weak"
        assert read_assurance["tier"] == "weak"
        assert write_assurance["caller_proven"] is False
        assert read_assurance["caller_proven"] is False


def test_sticky_cache_decay_agrees_on_the_new_sources():
    """A sticky-cache envelope decays the original proof one tier on both
    paths; the newly aligned sources must decay identically."""
    for source in ("agent_uuid_direct", "operator_token"):
        wrapped = f"sticky_cache:{source}"
        write_tier = write_path._compute_identity_assurance(wrapped, None)["tier"]
        read_tier = read_path._identity_assurance_from_source(wrapped)["tier"]
        assert write_tier == read_tier == "medium"
