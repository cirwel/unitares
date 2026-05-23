from src.services.identity_payloads import (
    build_identity_response_context,
    build_identity_diag_payload,
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
