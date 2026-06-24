from src.services.identity_payloads import (
    build_identity_response_context,
    build_identity_diag_payload,
    build_identity_signature_payload,
    build_identity_response_data,
    build_onboard_response_data,
)


def test_build_identity_response_data_verbose_includes_continuity_context():
    payload = build_identity_response_data(
        agent_uuid="uuid-123",
        agent_id="agent-123",
        display_name="Tester",
        client_session_id="sess-123",
        continuity_source="client_session_id",
        continuity_support={"enabled": True},
        continuity_token="token-abc",
        identity_status="resumed",
        identity_resolution_outcome="resumed",
        model_type="gpt",
        resumed=True,
        session_continuity=None,
        verbose=True,
    )

    assert payload["agent_id"] == "agent-123"
    assert payload["identity_resolution_outcome"] == "resumed"
    assert payload["continuity_token"] == "token-abc"
    assert payload["session_continuity"]["continuity_token"] == "token-abc"
    assert payload["quick_reference"]["for_path0_ownership_proof"] == "token-abc"
    assert payload["identity_assurance"]["tier"] == "strong"
    assert payload["identity_context"]["identity_is"] == "uuid"
    assert payload["identity_context"]["label_is"] == "social_or_cosmetic"
    assert payload["identity_context"]["harness_is"] == "context_not_identity_proof"
    assert payload["identity_context"]["continuity_claim"] == "resumed_by_explicit_session"
    # Doctrine: KG keys on agent_id, never on the cosmetic display_name.
    # The previous `display_name or agent_id` fallback leaked the cosmetic
    # label into a functional key path.
    assert payload["quick_reference"]["for_knowledge_graph"] == "agent-123"


def test_quick_reference_does_not_fall_back_to_display_name_for_kg():
    payload = build_identity_response_data(
        agent_uuid="uuid-xyz",
        agent_id="agent-xyz",
        display_name="CosmeticLabel",
        client_session_id="sess-xyz",
        continuity_source="client_session_id",
        continuity_support={"enabled": False},
        continuity_token=None,
        identity_status="active",
        model_type=None,
        resumed=None,
        session_continuity=None,
        verbose=True,
    )
    assert payload["quick_reference"]["for_knowledge_graph"] == "agent-xyz"
    assert payload["quick_reference"]["for_knowledge_graph"] != "CosmeticLabel"


def test_build_identity_diag_payload_keeps_fast_path_shape_consistent():
    payload = build_identity_diag_payload(
        agent_uuid="uuid-123",
        agent_id="agent-123",
        display_name="Tester",
        client_session_id="sess-123",
        continuity_source="client_session_id",
        continuity_support={"enabled": True},
        continuity_token="token-abc",
        identity_status="archived",
    )

    assert payload["identity_status"] == "archived"
    assert payload["bound_identity"]["uuid"] == "uuid-123"
    assert payload["continuity_token"] == "token-abc"
    assert payload["identity_context"]["registry"]["uuid"] == "uuid-123"
    assert payload["identity_context"]["label"]["is_identity_key"] is False


def test_build_identity_diag_payload_includes_principal_when_present(monkeypatch):
    """Regression: the diag fast-path folded in a derived principal but
    assigned it to `response_data` (a name from the sibling builder) instead
    of `payload`, raising NameError for any agent with a principal. Patch the
    lookup to return one and assert the payload carries it without raising."""
    monkeypatch.setattr(
        "src.services.identity_payloads._principal_lookup",
        lambda uuid: {"principal_id": "P-1", "role": "advisory"},
    )
    payload = build_identity_diag_payload(
        agent_uuid="uuid-123",
        agent_id="agent-123",
        display_name="Tester",
        client_session_id="sess-123",
        continuity_source="client_session_id",
        continuity_support={"enabled": True},
        continuity_token="token-abc",
        identity_status="resumed",
    )
    assert payload["principal"] == {"principal_id": "P-1", "role": "advisory"}


def test_build_onboard_response_data_includes_thread_and_workflow_when_verbose():
    payload = build_onboard_response_data(
        agent_uuid="uuid-123",
        structured_agent_id="agent-123",
        agent_label="Tester",
        stable_session_id="sess-123",
        is_new=True,
        force_new=False,
        client_hint="chatgpt",
        was_archived=False,
        trajectory_result={"genesis_stored": True},
        parent_agent_id=None,
        thread_context={
            "is_root": True,
            "thread_id": "thread-1234567890",
            "position": 1,
            "honest_message": "Root node",
        },
        verbose=True,
        continuity_source="continuity_token",
        identity_resolution_outcome="minted_after_resume_miss",
        continuity_support={"enabled": True},
        continuity_token="token-abc",
        system_activity={"agents": {"active": 1}},
        tool_mode_info={"current_mode": "lite"},
    )

    assert payload["continuity_token"] == "token-abc"
    assert payload["identity_resolution_outcome"] == "minted_after_resume_miss"
    assert payload["identity_assurance"]["tier"] == "strong"
    assert payload["identity_context"]["continuity_claim"] == "fresh_uuid_minted_after_resume_miss"
    assert payload["identity_context"]["harness_context"]["harness_type"] == "chatgpt"
    assert payload["thread_context"]["thread_id"] == "thread-1234567890"
    assert payload["workflow"]["step_1"] == "Copy client_session_id from above"
    assert payload["tool_mode"]["current_mode"] == "lite"
    assert payload["trajectory"]["trust_tier"]["tier"] == 1


def test_identity_response_context_distinguishes_uuid_label_harness_and_assurance():
    context = build_identity_response_context(
        agent_uuid="uuid-registry",
        agent_id="Gpt_5_Codex_20260506",
        display_name="Mnemos",
        session_resolution_source="pinned_onboard_session",
        identity_status="resumed",
        identity_resolution_outcome="resumed",
        client_hint="codex-cli",
        model_type="gpt-5.5",
    )

    assert context["schema"] == "s22.identity_response.v1"
    assert context["registry"] == {
        "uuid": "uuid-registry",
        "role": "registry_anchor",
        "is_identity_key": True,
    }
    assert context["public_handle"]["agent_id"] == "Gpt_5_Codex_20260506"
    assert context["public_handle"]["is_identity_key"] is False
    assert context["label"]["display_name"] == "Mnemos"
    assert context["label"]["is_identity_key"] is False
    assert context["harness_context"]["harness_type"] == "codex-cli"
    assert context["harness_context"]["model"] == "gpt-5.5"
    assert context["harness_context"]["is_identity_proof"] is False
    assert context["identity_assurance"]["tier"] == "medium"
    assert context["continuity_claim"] == "resumed_by_recent_onboard_pin"


def test_identity_signature_payload_uses_s22_contract():
    payload = build_identity_signature_payload(
        agent_uuid="uuid-sig",
        agent_id="Codex_20260622",
        display_name="codex-dispatch",
        label_source="claimed",
        session_resolution_source="explicit_client_session_id",
        proof_origin="caller_asserted",
    )

    assert payload["uuid"] == "uuid-sig"
    assert payload["agent_id"] == "Codex_20260622"
    assert payload["structured_agent_id"] == "Codex_20260622"
    assert payload["display_name"] == "codex-dispatch"
    assert payload["label_source"] == "claimed"
    assert payload["identity_context"]["schema"] == "s22.identity_response.v1"
    assert payload["identity_context"]["registry"]["uuid"] == "uuid-sig"
    assert payload["identity_context"]["public_handle"]["agent_id"] == "Codex_20260622"
    assert payload["identity_context"]["label"]["display_name"] == "codex-dispatch"
    assert payload["identity_assurance"]["tier"] == "strong"


# ── #679: server-injected fingerprint must not be laundered into strong ──
#
# On the stateless streamable transport (claude.ai remote connector), the
# typed wrapper injects `signals.ip_ua_fingerprint` as `client_session_id`
# when the caller omits one. Resolution then labels it
# `explicit_client_session_id` (a strong source). The write-path assurance
# (`phases._compute_identity_assurance`) already downgrades server-inferred
# bindings to weak via proof_origin; these tests pin the SAME honesty into
# the identity()/onboard RESPONSE assurance so the two surfaces agree.


def test_injected_explicit_csid_is_downgraded_to_weak_in_response():
    context = build_identity_response_context(
        agent_uuid="uuid-1",
        agent_id="agent-1",
        display_name=None,
        session_resolution_source="explicit_client_session_id",
        identity_status="resumed",
        proof_origin="server_inferred",
    )
    assurance = context["identity_assurance"]
    assert assurance["tier"] == "weak"
    assert assurance["score"] == 0.35
    assert assurance["caller_proven"] is False
    assert assurance["proof_origin"] == "server_inferred"


def test_server_inferred_overrides_even_a_strong_source_label():
    """proof_origin is authoritative over the source label — a strong label
    (mcp_session_id) resolved by server inference is still weak."""
    context = build_identity_response_context(
        agent_uuid="uuid-2",
        agent_id="agent-2",
        display_name=None,
        session_resolution_source="mcp_session_id",
        identity_status="resumed",
        proof_origin="server_inferred",
    )
    assert context["identity_assurance"]["tier"] == "weak"


def test_caller_asserted_explicit_csid_stays_strong():
    context = build_identity_response_context(
        agent_uuid="uuid-3",
        agent_id="agent-3",
        display_name=None,
        session_resolution_source="explicit_client_session_id",
        identity_status="resumed",
        proof_origin="caller_asserted",
    )
    assurance = context["identity_assurance"]
    assert assurance["tier"] == "strong"
    assert assurance["caller_proven"] is True


def test_unknown_proof_origin_leaves_tier_unchanged_backward_compat():
    """Legacy callers that don't thread provenance keep today's behavior
    (fail-open) — an explicit source with no proof_origin stays strong."""
    context = build_identity_response_context(
        agent_uuid="uuid-4",
        agent_id="agent-4",
        display_name=None,
        session_resolution_source="explicit_client_session_id",
        identity_status="resumed",
    )
    assurance = context["identity_assurance"]
    assert assurance["tier"] == "strong"
    assert assurance["caller_proven"] is False
    assert assurance["proof_origin"] == "unknown"


def test_onboard_response_threads_server_inferred_downgrade():
    """End-to-end through the onboard builder: an injected CSID surfaces as
    weak in the onboard() response identity_assurance."""
    payload = build_onboard_response_data(
        agent_uuid="uuid-5",
        structured_agent_id="agent-5",
        agent_label=None,
        stable_session_id="sess-5",
        is_new=False,
        force_new=False,
        client_hint="claude",
        was_archived=False,
        trajectory_result=None,
        parent_agent_id=None,
        thread_context=None,
        verbose=False,
        continuity_source="explicit_client_session_id",
        continuity_support={"enabled": False},
        continuity_token=None,
        system_activity=None,
        tool_mode_info=None,
        proof_origin="server_inferred",
    )
    assert payload["identity_assurance"]["tier"] == "weak"


# ── #732: assurance carries a how_to_strengthen breadcrumb for non-strong ──


def test_strong_assurance_has_no_strengthen_breadcrumb():
    """A caller-proven strong binding needs no action — the breadcrumb is
    omitted so the assurance block stays lean."""
    context = build_identity_response_context(
        agent_uuid="uuid-s",
        agent_id="agent-s",
        display_name=None,
        session_resolution_source="explicit_client_session_id",
        identity_status="resumed",
        proof_origin="caller_asserted",
    )
    assurance = context["identity_assurance"]
    assert assurance["tier"] == "strong"
    assert "how_to_strengthen" not in assurance


def test_weak_server_inferred_assurance_explains_how_to_reach_strong():
    """The canonical #732 case: weak because server-inferred. Post-#604 the
    breadcrumb LEADS with continuity_token (works on stateless transports) and
    still mentions client_session_id for session-maintaining clients."""
    context = build_identity_response_context(
        agent_uuid="uuid-w",
        agent_id="agent-w",
        display_name=None,
        session_resolution_source="server_inferred_binding",
        identity_status="resumed",
        proof_origin="server_inferred",
    )
    assurance = context["identity_assurance"]
    assert assurance["tier"] == "weak"
    hint = assurance["how_to_strengthen"]
    assert "server-inferred" in hint
    # #604: continuity_token is the primary proof (works on both transports).
    assert "continuity_token" in hint
    assert hint.index("continuity_token") < hint.index("client_session_id")


def test_medium_assurance_breadcrumb_points_at_explicit_session():
    context = build_identity_response_context(
        agent_uuid="uuid-m",
        agent_id="agent-m",
        display_name=None,
        session_resolution_source="pinned_onboard_session",
        identity_status="resumed",
    )
    assurance = context["identity_assurance"]
    assert assurance["tier"] == "medium"
    hint = assurance["how_to_strengthen"]
    # #604: continuity_token leads; client_session_id remains as the
    # session-maintaining-client alternative.
    assert "continuity_token" in hint
    assert "client_session_id" in hint
    assert hint.index("continuity_token") < hint.index("client_session_id")


def test_how_to_strengthen_read_and_write_paths_stay_in_parity():
    """The read-path (identity_payloads) and write-path (updates.phases)
    breadcrumbs are mirrored by contract — guard against drift."""
    from src.services.identity_payloads import _how_to_strengthen as read_path
    from src.mcp_handlers.updates.phases import _how_to_strengthen as write_path

    cases = [
        ("strong", "continuity_token", None),
        ("medium", "pinned_onboard_session", None),
        ("weak", "ip_ua_fingerprint", "server_inferred"),
        ("weak", "unknown", None),
    ]
    for tier, source_key, proof_origin in cases:
        assert read_path(tier, source_key, proof_origin) == write_path(
            tier, source_key, proof_origin
        )


# ── #604 (dogfood 2026-06-24): credential copy is stateless-transport-safe ──


def test_onboard_session_continuity_instruction_leads_with_continuity_token():
    """An agent that follows the onboard session_continuity instruction must be
    told to echo continuity_token (the proof that resolves on stateless
    transports), not client_session_id (which resolves to a fresh per-call
    identity there)."""
    payload = build_onboard_response_data(**_onboard_kwargs())
    instruction = payload["session_continuity"]["instruction"]
    assert "continuity_token" in instruction
    assert payload["session_continuity"]["continuity_token"] == "token-min"


def test_onboard_next_calls_thread_continuity_token_as_ownership_proof():
    """The verbose next_calls templates hand back continuity_token as the
    ownership proof so an agent copying args_full reaches strong on its 2nd
    call (P0 acceptance)."""
    payload = build_onboard_response_data(**_onboard_kwargs())
    for call in payload["next_calls"]:
        args_full = call["args_full"]
        assert args_full.get("continuity_token") == "token-min"
        assert "client_session_id" not in args_full


def test_fresh_mint_weak_binding_is_framed_as_baseline_not_deficiency():
    """P1: a just-minted identity that resolves weakly is relabeled as expected
    baseline with an actionable path (echo continuity_token), not a scold."""
    payload = build_onboard_response_data(
        **_onboard_kwargs(
            is_new=True,
            force_new=True,
            continuity_source="unknown",  # fresh mint has no prior proof → weak
            response_mode="minimal",
        )
    )
    assurance = payload["identity_assurance"]
    assert assurance["tier"] != "strong"
    assert assurance["baseline"] == "fresh_identity"
    assert "not a deficiency" in assurance["baseline_note"]
    assert "continuity_token" in assurance["how_to_strengthen"]


# ── #734: onboard response_mode="minimal" lean envelope ──


def _onboard_kwargs(**overrides):
    base = dict(
        agent_uuid="uuid-min",
        structured_agent_id="agent-min",
        agent_label="Tester",
        stable_session_id="sess-min",
        is_new=True,
        force_new=False,
        client_hint="claude",
        was_archived=False,
        trajectory_result={"genesis_stored": True},
        parent_agent_id=None,
        thread_context=None,
        verbose=True,
        continuity_source="client_session_id",
        continuity_support={"enabled": True},
        continuity_token="token-min",
        system_activity={"agents": {"active": 1}},
        tool_mode_info={"current_mode": "lite"},
        identity_resolution_outcome="minted_fresh",
    )
    base.update(overrides)
    return base


def test_onboard_minimal_mode_drops_nested_ontology_and_verbose_extras():
    payload = build_onboard_response_data(**_onboard_kwargs(response_mode="minimal"))

    # Lean essentials present.
    assert payload["response_mode"] == "minimal"
    assert payload["uuid"] == "uuid-min"
    assert payload["agent_id"] == "agent-min"
    assert payload["client_session_id"] == "sess-min"
    assert payload["identity_assurance"]["tier"] == "strong"
    assert payload["identity_resolution_outcome"] == "minted_fresh"
    assert payload["next_step"]
    # Functional fields retained.
    assert payload["continuity_token"] == "token-min"

    # The nested ontology and verbose extras are dropped — this is the #734 win.
    assert "identity_context" not in payload
    assert "session_continuity" not in payload
    assert "next_calls" not in payload
    assert "workflow" not in payload
    assert "tool_mode" not in payload
    assert "system_activity" not in payload


def test_onboard_full_mode_is_unchanged_default():
    """Default (full) keeps the complete envelope byte-compatibly — the
    top-level assurance mirror and the nested ontology both remain."""
    payload = build_onboard_response_data(**_onboard_kwargs())
    assert "response_mode" not in payload  # full mode does not stamp the key
    assert payload["identity_context"]["identity_is"] == "uuid"
    assert payload["identity_assurance"]["tier"] == "strong"
    assert payload["session_continuity"]["client_session_id"] == "sess-min"
