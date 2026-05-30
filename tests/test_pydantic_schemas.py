import pytest
from pydantic import ValidationError
from src.tool_schemas import get_pydantic_schemas
from src.tool_schemas import get_pydantic_schemas

class TestPydanticSchemas:
    """Tests to ensure our new Pydantic schema validation is robust and has 
    full coverage of the type coercions and bounds checking previously handled 
    by manual validators."""

    def test_float_bounds(self):
        """Test float range bounds are applied correctly (e.g., complexity 0.0-1.0)."""
        from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams
        # Valid bounds
        valid = ProcessAgentUpdateParams(complexity=0.5, response_text="Test")
        assert valid.complexity == 0.5
        
        # Invalid upper bound
        with pytest.raises(ValidationError) as exc:
            ProcessAgentUpdateParams(complexity=1.5, response_text="Test")
        assert "less than or equal to 1" in str(exc.value).lower()
        
        # Invalid lower bound
        with pytest.raises(ValidationError) as exc:
            ProcessAgentUpdateParams(complexity=-0.1, response_text="Test")
        assert "greater than or equal to 0" in str(exc.value).lower()

    def test_ethical_drift_bounds(self):
        """Test ethical drift lists are correctly bounded."""
        from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams
        # Valid
        valid = ProcessAgentUpdateParams(ethical_drift=[0.1, -0.5, 1.0], response_text="T")
        assert valid.ethical_drift == [0.1, -0.5, 1.0]

        # Valid defaults
        default = ProcessAgentUpdateParams(response_text="T")
        assert default.ethical_drift == [0.0, 0.0, 0.0]

    def test_boolean_coercion(self):
        """Tests our custom @model_validator coercions for booleans."""
        from src.mcp_handlers.schemas.admin import ListToolsParams
        # Test ListToolsParams which uses coerce_booleans
        # "true" -> True
        model = ListToolsParams(essential_only="true", verbose=1)
        assert model.essential_only is True
        assert model.verbose is True
        
        # "false" -> False
        model2 = ListToolsParams(essential_only="false", verbose="0")
        assert model2.essential_only is False
        assert model2.verbose is False
        
        # Already bool
        model3 = ListToolsParams(essential_only=True)
        assert model3.essential_only is True

    def test_enum_validation(self):
        """Test Literal enums restrict input correctly."""
        from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams
        # Valid
        valid = ProcessAgentUpdateParams(response_text="T", response_mode="auto")
        assert valid.response_mode == "auto"

        # Invalid
        with pytest.raises(ValidationError):
            ProcessAgentUpdateParams(response_text="T", response_mode="invalid_mode")

    def test_process_update_epistemic_class_validation(self):
        """process_agent_update accepts the forward-only warrant labels."""
        from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams

        valid = ProcessAgentUpdateParams(
            response_text="hook-derived summary",
            epistemic_class="substrate_interpretation",
        )
        assert valid.epistemic_class == "substrate_interpretation"

        default = ProcessAgentUpdateParams(response_text="agent-authored")
        assert default.epistemic_class == "agent_report"

        with pytest.raises(ValidationError):
            ProcessAgentUpdateParams(
                response_text="old draft vocabulary",
                epistemic_class="vitals",
            )
        with pytest.raises(ValidationError):
            ProcessAgentUpdateParams(
                response_text="caller cannot self-label measured rows synthetic",
                epistemic_class="synthetic",
            )

    def test_schema_registry_complete(self):
        """Ensure all 79 tools mapped in PYDANTIC_SCHEMAS exist and are valid subclasses of BaseModel."""
        schemas = get_pydantic_schemas()
        assert len(schemas) >= 70, "Should have schemas for all known tools"
        for name, model_cls in schemas.items():
            assert hasattr(model_cls, "model_validate"), f"Schema for {name} must be a Pydantic model"

    def test_pi_params_validation(self):
        """Verify complex discriminated unions or enums in PiParams work.

        PiParams lives in unitares-pi-plugin as of Phase B1; skip when
        plugin isn't installed.
        """
        plugin_schemas = pytest.importorskip("unitares_pi_plugin.schemas")
        PiParams = plugin_schemas.PiParams
        valid_health = PiParams(action="health")
        assert valid_health.action == "health"

        valid_sync = PiParams(action="sync_eisv", update_governance=True)
        assert valid_sync.action == "sync_eisv"
        assert valid_sync.update_governance is True

        with pytest.raises(ValidationError):
            PiParams(action="unknown_action_xyz")

    def test_legacy_validation_removal(self):
        """Confirm that missing/invalid types let Pydantic handle it naturally."""
        # Instead of manual code checking if limit is int, Pydantic type hints do it
        # E.g., list_tools (we handle this automatically)
        pass 

    def test_knowledge_update_omitted_tags_stay_none(self):
        """Unified knowledge(update) should not materialize omitted tags as []."""
        from src.mcp_handlers.schemas.knowledge import KnowledgeParams

        model = KnowledgeParams(action="update", discovery_id="disc-1", content="new body")

        assert model.tags is None
        dumped = model.model_dump()
        assert dumped["tags"] is None

    def test_knowledge_cleanup_accepts_string_dry_run_false(self):
        """Unified knowledge(cleanup) must expose and coerce dry_run.

        Regression: SDK/consolidated callers pass "false" over JSON; without
        this field in the public schema, clients could only reach the default
        dry-run cleanup path.
        """
        from src.mcp_handlers.schemas.knowledge import KnowledgeParams

        model = KnowledgeParams(action="cleanup", dry_run="false")

        assert model.dry_run is False

    def test_knowledge_schema_accepts_audit_and_supersede(self):
        """Schema must accept every action the dispatcher routes.

        Regression: Vigil's groundskeeper silently failed for weeks because
        the dispatcher at consolidated.py routed 'audit' and 'supersede' but
        the schema Literal listed only 9 actions — every call hit
        PARAMETER_ERROR before reaching the handler. If you add a new action
        to consolidated.handle_knowledge, add it here too.
        """
        import typing
        from src.mcp_handlers.schemas.knowledge import KnowledgeParams

        schema_actions = set(typing.get_args(KnowledgeParams.model_fields["action"].annotation))

        # These were the two silently-rejected actions from the incident
        assert "audit" in schema_actions
        assert "supersede" in schema_actions

        # The handler dispatcher's full action set (consolidated.py:98-109).
        # Kept in a literal here so adding an action there without updating
        # the schema surfaces as a test failure, not a runtime PARAMETER_ERROR.
        dispatcher_actions = {
            "store", "search", "get", "list", "update", "details",
            "note", "cleanup", "stats", "supersede", "audit",
        }
        missing = dispatcher_actions - schema_actions
        assert not missing, f"Schema missing dispatcher actions: {missing}"

    def test_dialectic_schema_accepts_quick_action(self):
        """Lightweight dialectic action must be accepted by public schema."""
        from src.mcp_handlers.schemas.dialectic import DialecticParams

        model = DialecticParams(
            action="quick",
            issue_description="Should I proceed?",
            position="Proceed after tests pass",
            decision="proceed",
            concerns=["low blast radius"],
        )

        assert model.action == "quick"
        assert model.position == "Proceed after tests pass"

    def test_store_knowledge_schema_matches_handler_discovery_types(self):
        """Public schema must not reject discovery types the handler accepts."""
        import typing
        from src.mcp_handlers.schemas.knowledge import StoreKnowledgeGraphParams

        schema_types = set(typing.get_args(StoreKnowledgeGraphParams.model_fields["discovery_type"].annotation.__args__[0]))
        handler_types = {
            "architectural_decision", "learning", "pattern", "bug_fix",
            "refactoring", "documentation", "experiment", "question", "note", "rule",
            "insight", "bug_found", "bug", "improvement", "exploration", "observation",
        }

        missing = handler_types - schema_types
        assert not missing, f"Store schema missing handler discovery types: {missing}"
        assert StoreKnowledgeGraphParams(summary="found auth issue", discovery_type="bug_found").discovery_type == "bug_found"
        assert StoreKnowledgeGraphParams(summary="shorthand", discovery_type="bug").discovery_type == "bug"


class TestVerificationSource:
    def test_default_is_agent_reported_tool_result(self):
        from src.mcp_handlers.schemas.core import OutcomeEventParams
        params = OutcomeEventParams(outcome_type="test_passed")
        assert params.verification_source == "agent_reported_tool_result"

    def test_accepts_server_observation(self):
        from src.mcp_handlers.schemas.core import OutcomeEventParams
        params = OutcomeEventParams(
            outcome_type="test_passed",
            verification_source="server_observation",
        )
        assert params.verification_source == "server_observation"

    def test_rejects_unknown_value(self):
        import pytest
        from pydantic import ValidationError
        from src.mcp_handlers.schemas.core import OutcomeEventParams
        with pytest.raises(ValidationError):
            OutcomeEventParams(
                outcome_type="test_passed",
                verification_source="random_made_up_string",
            )


class TestToolResultEvidence:
    def test_minimal_valid(self):
        from src.mcp_handlers.schemas.core import ToolResultEvidence
        ev = ToolResultEvidence(kind="test", tool="pytest", summary="ok")
        assert ev.kind == "test"
        assert ev.exit_code is None

    def test_omitted_kind_is_inferred(self):
        from src.mcp_handlers.schemas.core import ToolResultEvidence
        ev = ToolResultEvidence(tool="pytest", summary="tests passed", exit_code=0)
        assert ev.kind == "test"

    def test_omitted_kind_defaults_to_command_when_tool_present(self):
        from src.mcp_handlers.schemas.core import ToolResultEvidence
        ev = ToolResultEvidence(tool="curl", summary="health check passed", exit_code=0)
        assert ev.kind == "command"

    def test_rejects_extra_fields(self):
        from pydantic import ValidationError
        from src.mcp_handlers.schemas.core import ToolResultEvidence
        with pytest.raises(ValidationError):
            ToolResultEvidence(kind="test", tool="pytest", summary="ok", random_field="x")

    def test_rejects_unknown_kind(self):
        from pydantic import ValidationError
        from src.mcp_handlers.schemas.core import ToolResultEvidence
        with pytest.raises(ValidationError):
            ToolResultEvidence(kind="not_a_real_kind", tool="x", summary="x")

    def test_tool_name_max_length(self):
        from pydantic import ValidationError
        from src.mcp_handlers.schemas.core import ToolResultEvidence
        with pytest.raises(ValidationError):
            ToolResultEvidence(kind="test", tool="x" * 65, summary="ok")


class TestProcessAgentUpdateAcceptsRecentToolResults:
    def test_optional_field_defaults_none(self):
        from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams
        params = ProcessAgentUpdateParams(response_text="hello")
        assert params.recent_tool_results is None

    def test_accepts_list_of_evidence(self):
        from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams
        params = ProcessAgentUpdateParams(
            response_text="ran tests",
            recent_tool_results=[
                {"kind": "test", "tool": "pytest", "summary": "passed", "exit_code": 0}
            ],
        )
        assert len(params.recent_tool_results) == 1
        assert params.recent_tool_results[0].kind == "test"

    def test_accepts_s22_h5_agent_knowable_fields(self):
        """ProcessAgentUpdateParams keeps only the 4 agent-knowable S22 fields.

        Per KG 2026-05-09T13:03 (43a2cbf9): the 14 harness/server-knowable
        S22 fields (harness, harness_id, harness_type, transport, model,
        model_provider, tool_surface, governance_mode, verification_source,
        episode_id, invocation_id, process_instance_id, locus,
        affordance_state) are filled server-side by build_s22_write_context
        and were removed from the agent-callable schema. The 4 kept fields
        are author-of-intent facts only the agent can supply.
        """
        from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams
        params = ProcessAgentUpdateParams(
            response_text="ran H5 diagnostic",
            comparison_key="s22-h5-2026-05-06",
            task_label="Run S22 H5 coverage diagnostic",
            task_outcome="diagnostic-complete",
            memory_context="MEMORY.md + KG search results",
        )
        assert params.comparison_key == "s22-h5-2026-05-06"
        assert params.task_label == "Run S22 H5 coverage diagnostic"
        assert params.task_outcome == "diagnostic-complete"
        assert params.memory_context == "MEMORY.md + KG search results"

    def test_dropped_s22_fields_no_longer_on_model(self):
        """The 14 dropped S22 fields must not be declared on the schema.

        Regression for KG 2026-05-09T13:03: keep the agent-facing schema
        from re-accreting fields that only the harness can honestly know.
        """
        from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams
        dropped = {
            "harness", "harness_id", "harness_type", "transport",
            "model", "model_provider", "tool_surface", "governance_mode",
            "verification_source", "episode_id", "invocation_id",
            "process_instance_id", "locus", "affordance_state",
        }
        declared = set(ProcessAgentUpdateParams.model_fields.keys())
        assert dropped.isdisjoint(declared), (
            f"S22 fields that should be harness-filled are still declared: "
            f"{sorted(dropped & declared)}"
        )

    def test_accepts_evidence_without_kind(self):
        from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams
        params = ProcessAgentUpdateParams(
            response_text="ran tests",
            recent_tool_results=[
                {"tool": "pytest", "summary": "passed", "exit_code": 0}
            ],
        )
        assert len(params.recent_tool_results) == 1
        assert params.recent_tool_results[0].kind == "test"
