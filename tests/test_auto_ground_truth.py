"""
Tests for src/auto_ground_truth.py - Automated Ground Truth Collection

Tests the objective outcome evaluators (pure functions, no external deps)
and the async collect_ground_truth_automatically function (mocked deps).
"""

import pytest
import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import tempfile
import os

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.auto_ground_truth import (
    evaluate_test_outcome,
    evaluate_command_outcome,
    evaluate_file_operation,
    evaluate_lint_outcome,
    evaluate_objective_outcomes,
    evaluate_decision_outcome,
    has_exogenous_signals,
    collect_ground_truth_automatically,
)


# =============================================================================
# evaluate_test_outcome Tests
# =============================================================================


class TestEvaluateTestOutcome:
    """Tests for evaluate_test_outcome()."""

    def test_none_input(self):
        assert evaluate_test_outcome(None) is None

    def test_empty_dict(self):
        assert evaluate_test_outcome({}) is None

    def test_exit_code_zero(self):
        assert evaluate_test_outcome({"exit_code": 0}) is True

    def test_exit_code_one(self):
        assert evaluate_test_outcome({"exit_code": 1}) is False

    def test_exit_code_other_nonzero(self):
        """Other non-zero exit codes also mean failure."""
        assert evaluate_test_outcome({"exit_code": 2}) is False
        assert evaluate_test_outcome({"exit_code": 127}) is False

    def test_passed_count_positive(self):
        assert evaluate_test_outcome({"passed": 10, "failed": 0}) is True

    def test_passed_count_only(self):
        """Only passed count, no failed key."""
        assert evaluate_test_outcome({"passed": 5}) is True

    def test_failed_count(self):
        assert evaluate_test_outcome({"passed": 5, "failed": 2}) is False

    def test_errors_count(self):
        assert evaluate_test_outcome({"passed": 5, "errors": 1}) is False

    def test_failed_only_no_passed(self):
        """Failed tests with no passed count."""
        assert evaluate_test_outcome({"failed": 3}) is False

    def test_errors_only_no_passed(self):
        """Errors with no passed or failed count."""
        assert evaluate_test_outcome({"errors": 2}) is False

    def test_exit_code_takes_priority_over_failed(self):
        """exit_code=0 overrides failed count."""
        assert evaluate_test_outcome({"exit_code": 0, "failed": 5}) is True

    def test_exit_code_takes_priority_over_passed(self):
        """exit_code=1 overrides passed count."""
        assert evaluate_test_outcome({"exit_code": 1, "passed": 10}) is False

    def test_no_test_results_other_keys(self):
        """Dict with unrelated keys returns None."""
        assert evaluate_test_outcome({"other": "data"}) is None

    def test_all_zeros(self):
        """passed=0, failed=0, errors=0 and no exit_code returns None."""
        assert evaluate_test_outcome({"passed": 0, "failed": 0, "errors": 0}) is None


# =============================================================================
# evaluate_command_outcome Tests
# =============================================================================


class TestEvaluateCommandOutcome:
    """Tests for evaluate_command_outcome()."""

    def test_none_input(self):
        assert evaluate_command_outcome(None) is None

    def test_empty_dict(self):
        assert evaluate_command_outcome({}) is None

    def test_success_true(self):
        assert evaluate_command_outcome({"success": True}) is True

    def test_success_false(self):
        assert evaluate_command_outcome({"success": False}) is False

    def test_success_truthy_int(self):
        """success=1 should be truthy."""
        assert evaluate_command_outcome({"success": 1}) is True

    def test_success_falsy_zero(self):
        """success=0 should be falsy."""
        assert evaluate_command_outcome({"success": 0}) is False

    def test_exit_code_zero(self):
        assert evaluate_command_outcome({"exit_code": 0}) is True

    def test_exit_code_nonzero(self):
        assert evaluate_command_outcome({"exit_code": 2}) is False

    def test_exit_code_one(self):
        assert evaluate_command_outcome({"exit_code": 1}) is False

    def test_error_field_string(self):
        assert evaluate_command_outcome({"error": "something broke"}) is False

    def test_error_field_empty_string(self):
        """Empty error string is falsy, so returns None."""
        assert evaluate_command_outcome({"error": ""}) is None

    def test_error_field_none_value(self):
        """error=None is falsy, returns None."""
        assert evaluate_command_outcome({"error": None}) is None

    def test_success_flag_takes_priority_over_error(self):
        """success is checked before error field."""
        assert evaluate_command_outcome({"success": True, "error": "ignored"}) is True

    def test_success_flag_takes_priority_over_exit_code(self):
        """success is checked before exit_code."""
        assert evaluate_command_outcome({"success": True, "exit_code": 1}) is True
        assert evaluate_command_outcome({"success": False, "exit_code": 0}) is False

    def test_exit_code_takes_priority_over_error(self):
        """exit_code is checked before error field."""
        assert evaluate_command_outcome({"exit_code": 0, "error": "some error"}) is True

    def test_no_relevant_keys(self):
        """Dict with unrelated keys returns None."""
        assert evaluate_command_outcome({"stdout": "output", "duration": 1.5}) is None


# =============================================================================
# evaluate_file_operation Tests
# =============================================================================


class TestEvaluateFileOperation:
    """Tests for evaluate_file_operation()."""

    def test_existing_file_expected_exists(self):
        assert evaluate_file_operation(__file__, expected_exists=True) is True

    def test_existing_file_expected_not_exists(self):
        assert evaluate_file_operation(__file__, expected_exists=False) is False

    def test_nonexistent_file_expected_exists(self):
        assert evaluate_file_operation("/nonexistent/path/xyz.txt", expected_exists=True) is False

    def test_nonexistent_file_expected_not_exists(self):
        assert evaluate_file_operation("/nonexistent/path/xyz.txt", expected_exists=False) is True

    def test_with_temp_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            assert evaluate_file_operation(path, expected_exists=True) is True
            assert evaluate_file_operation(path, expected_exists=False) is False
        finally:
            os.unlink(path)

    def test_after_temp_file_deleted(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        os.unlink(path)
        assert evaluate_file_operation(path, expected_exists=True) is False
        assert evaluate_file_operation(path, expected_exists=False) is True

    def test_directory_exists(self):
        assert evaluate_file_operation(str(Path(__file__).parent), expected_exists=True) is True

    def test_default_expected_exists(self):
        assert evaluate_file_operation(__file__) is True

    def test_with_tmp_path(self, tmp_path):
        test_file = tmp_path / "test_file.txt"
        test_file.write_text("hello")
        assert evaluate_file_operation(str(test_file), expected_exists=True) is True
        assert evaluate_file_operation(str(test_file), expected_exists=False) is False


# =============================================================================
# evaluate_lint_outcome Tests
# =============================================================================


class TestEvaluateLintOutcome:
    """Tests for evaluate_lint_outcome()."""

    def test_none_input(self):
        assert evaluate_lint_outcome(None) is None

    def test_empty_dict(self):
        assert evaluate_lint_outcome({}) is None

    def test_no_errors(self):
        assert evaluate_lint_outcome({"errors": 0, "warnings": 3}) is True

    def test_errors_present(self):
        assert evaluate_lint_outcome({"errors": 5}) is False

    def test_single_error(self):
        assert evaluate_lint_outcome({"errors": 1}) is False

    def test_error_count_key(self):
        assert evaluate_lint_outcome({"error_count": 2}) is False

    def test_error_count_zero(self):
        assert evaluate_lint_outcome({"error_count": 0, "status": "ok"}) is True

    def test_errors_as_list_nonempty(self):
        assert evaluate_lint_outcome({"errors": ["err1", "err2"]}) is False

    def test_errors_as_list_single(self):
        assert evaluate_lint_outcome({"errors": ["err1"]}) is False

    def test_errors_as_empty_list(self):
        assert evaluate_lint_outcome({"errors": []}) is True

    def test_warnings_only(self):
        assert evaluate_lint_outcome({"warnings": 10}) is True

    def test_result_with_only_status(self):
        assert evaluate_lint_outcome({"status": "clean"}) is True

    def test_errors_key_takes_priority_over_error_count(self):
        assert evaluate_lint_outcome({"errors": 0, "error_count": 5}) is True
        assert evaluate_lint_outcome({"errors": 3, "error_count": 0}) is False


# =============================================================================
# evaluate_objective_outcomes Tests (Composite)
# =============================================================================


class TestEvaluateObjectiveOutcomes:
    """Tests for evaluate_objective_outcomes() composite evaluator."""

    def test_empty_outcomes(self):
        assert evaluate_objective_outcomes({}) is None

    def test_no_recognized_keys(self):
        assert evaluate_objective_outcomes({"unrelated": "data"}) is None

    def test_all_pass(self):
        outcomes = {
            "tests": {"exit_code": 0},
            "commands": {"success": True},
            "lint": {"errors": 0},
        }
        assert evaluate_objective_outcomes(outcomes) is True

    def test_all_pass_with_files(self):
        outcomes = {
            "tests": {"exit_code": 0},
            "commands": {"success": True},
            "lint": {"errors": 0},
            "files": [{"path": __file__, "expected_exists": True}],
        }
        assert evaluate_objective_outcomes(outcomes) is True

    def test_any_failure_fails_command(self):
        outcomes = {
            "tests": {"exit_code": 0},
            "commands": {"success": False},
            "lint": {"errors": 0},
        }
        assert evaluate_objective_outcomes(outcomes) is False

    def test_any_failure_fails_test(self):
        assert evaluate_objective_outcomes({"tests": {"exit_code": 1}}) is False

    def test_any_failure_fails_lint(self):
        outcomes = {"tests": {"exit_code": 0}, "lint": {"errors": 3}}
        assert evaluate_objective_outcomes(outcomes) is False

    def test_command_single_dict(self):
        assert evaluate_objective_outcomes({"commands": {"exit_code": 0}}) is True

    def test_command_list_all_pass(self):
        outcomes = {"commands": [{"exit_code": 0}, {"exit_code": 0}]}
        assert evaluate_objective_outcomes(outcomes) is True

    def test_command_list_with_failure(self):
        outcomes = {"commands": [{"exit_code": 0}, {"exit_code": 1}]}
        assert evaluate_objective_outcomes(outcomes) is False

    def test_command_list_all_fail(self):
        outcomes = {"commands": [{"exit_code": 1}, {"success": False}]}
        assert evaluate_objective_outcomes(outcomes) is False

    def test_file_checks_pass(self):
        outcomes = {"files": [{"path": __file__, "expected_exists": True}]}
        assert evaluate_objective_outcomes(outcomes) is True

    def test_file_checks_failure(self):
        outcomes = {"files": [{"path": "/nonexistent/xyz.txt", "expected_exists": True}]}
        assert evaluate_objective_outcomes(outcomes) is False

    def test_file_checks_multiple_all_pass(self):
        outcomes = {
            "files": [
                {"path": __file__, "expected_exists": True},
                {"path": "/nonexistent/xyz.txt", "expected_exists": False},
            ]
        }
        assert evaluate_objective_outcomes(outcomes) is True

    def test_file_checks_multiple_one_fails(self):
        outcomes = {
            "files": [
                {"path": __file__, "expected_exists": True},
                {"path": "/nonexistent/xyz.txt", "expected_exists": True},
            ]
        }
        assert evaluate_objective_outcomes(outcomes) is False

    def test_file_check_no_path_skipped(self):
        outcomes = {"files": [{"expected_exists": True}]}
        assert evaluate_objective_outcomes(outcomes) is None

    def test_unevaluable_signals_ignored(self):
        outcomes = {"tests": {}, "commands": {"exit_code": 0}}
        assert evaluate_objective_outcomes(outcomes) is True

    def test_all_unevaluable(self):
        outcomes = {"tests": {}, "commands": {}}
        assert evaluate_objective_outcomes(outcomes) is None

    def test_only_tests(self):
        assert evaluate_objective_outcomes({"tests": {"passed": 5}}) is True

    def test_only_lint(self):
        assert evaluate_objective_outcomes({"lint": {"errors": 0, "warnings": 2}}) is True


# =============================================================================
# evaluate_decision_outcome Tests (Calibration)
# =============================================================================


class TestEvaluateDecisionOutcome:
    """Tests for evaluate_decision_outcome() calibration evaluator."""

    def test_no_confidence_returns_none(self):
        entry = {"agent_id": "a1"}
        metadata = {"a1": {"status": "active"}}
        assert evaluate_decision_outcome(entry, metadata) is None

    def test_no_confidence_in_details_returns_none(self):
        entry = {"agent_id": "a1", "details": {"other": 1}}
        metadata = {"a1": {"status": "active"}}
        assert evaluate_decision_outcome(entry, metadata) is None

    def test_unknown_agent_returns_none(self):
        entry = {"confidence": 0.8, "agent_id": "a1"}
        assert evaluate_decision_outcome(entry, {}) is None

    def test_unknown_status_returns_none(self):
        entry = {"confidence": 0.8, "agent_id": "a1"}
        metadata = {"a1": {"status": "some_unknown_status"}}
        assert evaluate_decision_outcome(entry, metadata) is None

    def test_missing_agent_id_defaults_to_unknown(self):
        entry = {"confidence": 0.7}
        metadata = {"a1": {"status": "active"}}
        assert evaluate_decision_outcome(entry, metadata) is None

    def test_paused_agent_high_confidence_overconfident(self):
        entry = {"confidence": 0.9, "agent_id": "a1"}
        metadata = {"a1": {"status": "paused"}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_paused_agent_low_confidence_well_calibrated(self):
        entry = {"confidence": 0.2, "agent_id": "a1"}
        metadata = {"a1": {"status": "paused"}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_paused_agent_moderate_confidence(self):
        entry = {"confidence": 0.5, "agent_id": "a1"}
        metadata = {"a1": {"status": "paused"}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_paused_agent_near_threshold_under(self):
        """confidence=0.54, outcome_quality=0.2, gap=0.34 < 0.35 -> True."""
        entry = {"confidence": 0.54, "agent_id": "a1"}
        metadata = {"a1": {"status": "paused"}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_paused_agent_just_over_threshold(self):
        """confidence=0.56, outcome_quality=0.2, gap=0.36 > 0.35 -> False."""
        entry = {"confidence": 0.56, "agent_id": "a1"}
        metadata = {"a1": {"status": "paused"}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_paused_agent_float_precision_at_boundary(self):
        """confidence=0.55 - 0.2 = 0.35000000000000003 due to float precision -> False."""
        entry = {"confidence": 0.55, "agent_id": "a1"}
        metadata = {"a1": {"status": "paused"}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_loop_detected_counts_as_poor_outcome(self):
        entry = {"confidence": 0.8, "agent_id": "a1"}
        metadata = {"a1": {"status": "active", "loop_detected_at": "2026-01-01"}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_paused_at_counts_as_poor_outcome(self):
        entry = {"confidence": 0.8, "agent_id": "a1"}
        metadata = {"a1": {"status": "active", "paused_at": "2026-01-01T12:00:00"}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_active_agent_appropriate_confidence(self):
        entry = {"confidence": 0.7, "agent_id": "a1"}
        metadata = {"a1": {"status": "active", "update_count": 5}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_active_agent_zero_updates(self):
        entry = {"confidence": 0.7, "agent_id": "a1"}
        metadata = {"a1": {"status": "active", "update_count": 0}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_active_agent_no_update_count_key(self):
        entry = {"confidence": 0.7, "agent_id": "a1"}
        metadata = {"a1": {"status": "active"}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_active_agent_very_low_confidence_underconfident(self):
        entry = {"confidence": 0.2, "agent_id": "a1"}
        metadata = {"a1": {"status": "active", "update_count": 0}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_active_agent_very_high_confidence(self):
        entry = {"confidence": 1.0, "agent_id": "a1"}
        metadata = {"a1": {"status": "active", "update_count": 0}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_experience_bonus(self):
        entry = {"confidence": 0.85, "agent_id": "a1"}
        metadata = {"a1": {"status": "active", "update_count": 15}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_experience_bonus_capped(self):
        entry = {"confidence": 0.85, "agent_id": "a1"}
        metadata = {"a1": {"status": "active", "update_count": 100}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_archived_agent_high_confidence(self):
        entry = {"confidence": 0.85, "agent_id": "a1"}
        metadata = {"a1": {"status": "archived"}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_archived_agent_very_high_confidence(self):
        entry = {"confidence": 0.95, "agent_id": "a1"}
        metadata = {"a1": {"status": "archived"}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_archived_agent_low_confidence_underconfident(self):
        entry = {"confidence": 0.3, "agent_id": "a1"}
        metadata = {"a1": {"status": "archived"}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_archived_agent_at_threshold_boundary(self):
        entry = {"confidence": 0.6, "agent_id": "a1"}
        metadata = {"a1": {"status": "archived"}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_archived_agent_just_below_boundary(self):
        entry = {"confidence": 0.59, "agent_id": "a1"}
        metadata = {"a1": {"status": "archived"}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_waiting_input_moderate_confidence(self):
        entry = {"confidence": 0.6, "agent_id": "a1"}
        metadata = {"a1": {"status": "waiting_input"}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_waiting_input_high_confidence_overconfident(self):
        entry = {"confidence": 0.99, "agent_id": "a1"}
        metadata = {"a1": {"status": "waiting_input"}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_waiting_input_low_confidence(self):
        entry = {"confidence": 0.4, "agent_id": "a1"}
        metadata = {"a1": {"status": "waiting_input"}}
        assert evaluate_decision_outcome(entry, metadata) is True

    def test_confidence_from_details(self):
        entry = {"agent_id": "a1", "details": {"confidence": 0.7}}
        metadata = {"a1": {"status": "active", "update_count": 5}}
        assert evaluate_decision_outcome(entry, metadata) is not None

    def test_legacy_coherence_does_not_substitute_for_confidence(self):
        entry = {"agent_id": "a1", "details": {"coherence": 0.7}}
        metadata = {"a1": {"status": "active", "update_count": 0}}
        assert evaluate_decision_outcome(entry, metadata) is None

    def test_top_level_confidence_over_details(self):
        entry = {"confidence": 0.9, "agent_id": "a1", "details": {"confidence": 0.2}}
        metadata = {"a1": {"status": "paused"}}
        assert evaluate_decision_outcome(entry, metadata) is False

    def test_confidence_as_string_converted(self):
        entry = {"confidence": "0.7", "agent_id": "a1"}
        metadata = {"a1": {"status": "active", "update_count": 0}}
        assert evaluate_decision_outcome(entry, metadata) is True


# =============================================================================
# collect_ground_truth_automatically Tests (Async, Mocked)
# =============================================================================


def _make_mocks(entries, agent_metadata_dict, use_dict_metadata=False):
    mock_checker = MagicMock()
    mock_checker.bin_stats = {}
    mock_checker.reset = MagicMock()
    mock_checker.save_state = MagicMock()
    mock_checker.update_ground_truth = MagicMock()

    mock_audit = MagicMock()
    mock_audit.query_audit_log = MagicMock(return_value=entries)

    mock_mcp_server = MagicMock()
    mock_mcp_server.load_metadata_async = AsyncMock()

    if use_dict_metadata:
        mock_mcp_server.agent_metadata = agent_metadata_dict
    else:
        mock_mcp_server.agent_metadata = {
            aid: MagicMock(to_dict=MagicMock(return_value=meta))
            for aid, meta in agent_metadata_dict.items()
        }

    return mock_checker, mock_audit, mock_mcp_server


def _patch_modules(mock_checker, mock_audit, mock_mcp_server):
    """
    Patch sys.modules AND parent package attributes so that
    collect_ground_truth_automatically's runtime imports resolve to our mocks.

    Key subtlety: `import src.mcp_server_std as X` resolves via the parent
    package's __dict__ (src.agent_state attribute), NOT just sys.modules.
    `from src.calibration import Y` resolves via sys.modules. We must patch both.
    """
    import contextlib
    import src as _src_pkg

    @contextlib.contextmanager
    def ctx():
        mock_calibration = MagicMock(calibration_checker=mock_checker)
        mock_audit_log = MagicMock(AuditLogger=MagicMock(return_value=mock_audit))

        # Save originals from the parent package
        orig_mcp = getattr(_src_pkg, 'agent_state', None)
        orig_cal = getattr(_src_pkg, 'calibration', None)
        orig_aud = getattr(_src_pkg, 'audit_log', None)

        with patch.dict(sys.modules, {
            "src.calibration": mock_calibration,
            "src.agent_state": mock_mcp_server,
            "src.audit_log": mock_audit_log,
        }):
            # Also patch the parent package attributes (for `import src.X as Y`)
            _src_pkg.agent_state = mock_mcp_server
            _src_pkg.calibration = mock_calibration
            _src_pkg.audit_log = mock_audit_log
            try:
                yield
            finally:
                # Restore parent package attributes
                if orig_mcp is not None:
                    _src_pkg.agent_state = orig_mcp
                elif hasattr(_src_pkg, 'agent_state'):
                    delattr(_src_pkg, 'agent_state')
                if orig_cal is not None:
                    _src_pkg.calibration = orig_cal
                if orig_aud is not None:
                    _src_pkg.audit_log = orig_aud

    return ctx()


class TestHasExogenousSignals:
    """Tests for the hard gate that prevents calibration without exogenous data."""

    def test_empty_entry(self):
        assert has_exogenous_signals({}) is False

    def test_no_details_key(self):
        assert has_exogenous_signals({"confidence": 0.8}) is False

    def test_details_not_dict(self):
        assert has_exogenous_signals({"details": "string"}) is False
        assert has_exogenous_signals({"details": 42}) is False
        assert has_exogenous_signals({"details": None}) is False

    def test_empty_details(self):
        assert has_exogenous_signals({"details": {}}) is False

    def test_tests_signal(self):
        assert has_exogenous_signals({"details": {"tests": [{"passed": 5}]}}) is True

    def test_commands_signal(self):
        assert has_exogenous_signals({"details": {"commands": [{"exit_code": 0}]}}) is True

    def test_files_signal(self):
        assert has_exogenous_signals({"details": {"files": [{"path": "x.py"}]}}) is True

    def test_lint_signal(self):
        assert has_exogenous_signals({"details": {"lint": {"errors": 0}}}) is True

    def test_tool_usage_signal(self):
        assert has_exogenous_signals({"details": {"tool_usage": [{"tool": "read"}]}}) is True

    def test_tool_results_signal(self):
        assert has_exogenous_signals({"details": {"tool_results": [{"ok": True}]}}) is True

    def test_outcome_events_signal(self):
        assert has_exogenous_signals({"details": {"outcome_events": [{"type": "test"}]}}) is True

    def test_empty_lists_are_falsy(self):
        assert has_exogenous_signals({"details": {"tests": [], "commands": []}}) is False

    def test_only_endogenous_data(self):
        # EISV trajectory data is NOT exogenous
        assert has_exogenous_signals({"details": {"E": 0.8, "I": 0.7, "S": 0.1}}) is False

    def test_mixed_endogenous_and_exogenous(self):
        entry = {"details": {"E": 0.8, "commands": [{"exit_code": 0}]}}
        assert has_exogenous_signals(entry) is True


class TestCollectGroundTruthAutomatically:
    """Tests for the async collect_ground_truth_automatically function."""

    @pytest.mark.asyncio
    async def test_dry_run_no_entries(self):
        mock_checker, mock_audit, mock_mcp = _make_mocks([], {})
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=True)
        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == 0
        mock_checker.save_state.assert_not_called()
        mock_checker.update_ground_truth.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_with_entries(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"commands": [{"exit_code": 0}]}},
            {"timestamp": "2026-01-01T01:00:00", "confidence": 0.7, "agent_id": "a2",
             "details": {"tests": {"passed": 5, "failed": 0}}},
        ]
        metadata = {
            "a1": {"status": "active", "update_count": 5},
            "a2": {"status": "archived"},
        }
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=True)
        assert result["dry_run"] is True
        assert result["updated"] == 2
        assert result["errors"] == 0
        mock_checker.update_ground_truth.assert_not_called()
        mock_checker.save_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_run_updates_calibration(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"commands": [{"exit_code": 0}]}},
        ]
        metadata = {"a1": {"status": "active", "update_count": 3}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=False)
        assert result["dry_run"] is False
        assert result["updated"] == 1
        mock_checker.update_ground_truth.assert_called_once()
        mock_checker.save_state.assert_called_once()
        call_args = mock_checker.update_ground_truth.call_args
        assert call_args.kwargs["confidence"] == 0.8
        assert call_args.kwargs["predicted_correct"] is True

    @pytest.mark.asyncio
    async def test_live_run_low_confidence_prediction(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.3, "agent_id": "a1",
             "details": {"commands": [{"exit_code": 1}]}},
        ]
        metadata = {"a1": {"status": "active", "update_count": 0}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            await collect_ground_truth_automatically(dry_run=False)
        call_args = mock_checker.update_ground_truth.call_args
        assert call_args.kwargs["confidence"] == 0.3
        assert call_args.kwargs["predicted_correct"] is False

    @pytest.mark.asyncio
    async def test_skips_entries_without_confidence(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "agent_id": "unknown_agent"},
        ]
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, {})
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=False)
        assert result["skipped"] == 1
        assert result["updated"] == 0
        mock_checker.update_ground_truth.assert_not_called()
        mock_checker.save_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_deduplicates_timestamps(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"tool_usage": True}},
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"tool_usage": True}},
            {"timestamp": "2026-01-01T01:00:00", "confidence": 0.7, "agent_id": "a1",
             "details": {"tool_usage": True}},
        ]
        metadata = {"a1": {"status": "active", "update_count": 5}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=False)
        assert result["processed"] == 2
        assert result["updated"] == 2

    @pytest.mark.asyncio
    async def test_entries_without_timestamp_skipped(self):
        entries = [{"confidence": 0.8, "agent_id": "a1"}]
        metadata = {"a1": {"status": "active"}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=False)
        assert result["processed"] == 0
        assert result["updated"] == 0

    @pytest.mark.asyncio
    async def test_max_decisions_limits_processing(self):
        entries = [
            {"timestamp": f"2026-01-01T{i:02d}:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"tool_usage": True}}
            for i in range(10)
        ]
        metadata = {"a1": {"status": "active", "update_count": 5}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(max_decisions=3, dry_run=False)
        assert result["processed"] == 3
        assert result["updated"] == 3

    @pytest.mark.asyncio
    async def test_rebuild_resets_calibration(self):
        entries = [
            {"timestamp": f"2026-01-01T{i:02d}:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"tool_usage": True}}
            for i in range(5)
        ]
        metadata = {"a1": {"status": "active", "update_count": 5}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(
                rebuild=True, dry_run=False, max_decisions=2
            )
        mock_checker.reset.assert_called_once()
        mock_checker.save_state.assert_called()
        assert result["processed"] == 5
        assert result["updated"] == 5

    @pytest.mark.asyncio
    async def test_rebuild_dry_run_does_not_reset(self):
        mock_checker, mock_audit, mock_mcp = _make_mocks([], {})
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            await collect_ground_truth_automatically(rebuild=True, dry_run=True)
        mock_checker.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_in_entry_processing_increments_errors(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"commands": [{"exit_code": 0}]}},
        ]
        metadata = {"a1": {"status": "active", "update_count": 5}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        mock_checker.update_ground_truth.side_effect = Exception("boom")
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=False)
        assert result["errors"] == 1
        assert result["updated"] == 0

    @pytest.mark.asyncio
    async def test_metadata_dict_passthrough(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"tool_usage": True}},
        ]
        metadata = {"a1": {"status": "active", "update_count": 5}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata, use_dict_metadata=True)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=False)
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_no_save_when_no_updates(self):
        entries = [{"timestamp": "2026-01-01T00:00:00", "agent_id": "unknown"}]
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, {})
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=False)
        assert result["updated"] == 0
        mock_checker.save_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_metadata_fallback_to_json(self, tmp_path):
        mock_checker = MagicMock()
        mock_checker.bin_stats = {}
        mock_checker.update_ground_truth = MagicMock()
        mock_checker.save_state = MagicMock()

        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"commands": [{"exit_code": 0}]}},
        ]
        mock_audit = MagicMock()
        mock_audit.query_audit_log = MagicMock(return_value=entries)

        metadata = {"a1": {"status": "active", "update_count": 3}}
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "agent_metadata.json").write_text(json.dumps(metadata))

        # Mock mcp_server_std to raise ImportError so the fallback path is tested
        mock_mcp = MagicMock()
        mock_mcp.load_metadata_async = AsyncMock(side_effect=Exception("import failed"))

        import src.auto_ground_truth as agt_module
        original_root = agt_module.project_root
        agt_module.project_root = tmp_path
        try:
            with _patch_modules(mock_checker, mock_audit, mock_mcp):
                result = await collect_ground_truth_automatically(dry_run=False)
            assert result["updated"] == 1
        finally:
            agt_module.project_root = original_root

    @pytest.mark.asyncio
    async def test_metadata_fallback_no_json_file(self, tmp_path):
        mock_checker = MagicMock()
        mock_checker.bin_stats = {}
        mock_audit = MagicMock()

        # Mock mcp_server_std to raise ImportError so the fallback path is tested
        mock_mcp = MagicMock()
        mock_mcp.load_metadata_async = AsyncMock(side_effect=Exception("import failed"))

        import src.auto_ground_truth as agt_module
        original_root = agt_module.project_root
        agt_module.project_root = tmp_path
        try:
            with _patch_modules(mock_checker, mock_audit, mock_mcp):
                result = await collect_ground_truth_automatically(dry_run=False)
            assert result["updated"] == 0
            assert result["skipped"] == 0
            assert result["errors"] == 0
        finally:
            agt_module.project_root = original_root

    @pytest.mark.asyncio
    async def test_mixed_evaluable_and_skipped_entries(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.8, "agent_id": "a1",
             "details": {"commands": [{"exit_code": 0}]}},
            {"timestamp": "2026-01-01T01:00:00", "agent_id": "unknown"},  # no exogenous signals
            {"timestamp": "2026-01-01T02:00:00", "confidence": 0.9, "agent_id": "a2",
             "details": {"tests": {"passed": 1, "failed": 0}}},
        ]
        metadata = {
            "a1": {"status": "active", "update_count": 5},
            "a2": {"status": "paused"},
        }
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            result = await collect_ground_truth_automatically(dry_run=False)
        assert result["processed"] == 3
        assert result["updated"] == 2
        assert result["skipped"] == 1
        assert result["errors"] == 0

    @pytest.mark.asyncio
    async def test_query_uses_correct_event_type(self):
        mock_checker, mock_audit, mock_mcp = _make_mocks([], {})
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            await collect_ground_truth_automatically(dry_run=True)
        mock_audit.query_audit_log.assert_called_once()
        call_kwargs = mock_audit.query_audit_log.call_args
        assert call_kwargs.kwargs.get("event_type") == "auto_attest"

    @pytest.mark.asyncio
    async def test_actual_correct_reflects_overconfidence(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.9, "agent_id": "a1",
             "details": {"commands": [{"exit_code": 1}]}},
        ]
        metadata = {"a1": {"status": "paused"}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            await collect_ground_truth_automatically(dry_run=False)
        call_args = mock_checker.update_ground_truth.call_args
        assert call_args.kwargs["actual_correct"] is False

    @pytest.mark.asyncio
    async def test_actual_correct_true_for_well_calibrated(self):
        entries = [
            {"timestamp": "2026-01-01T00:00:00", "confidence": 0.85, "agent_id": "a1",
             "details": {"tests": {"passed": 10, "failed": 0}}},
        ]
        metadata = {"a1": {"status": "archived"}}
        mock_checker, mock_audit, mock_mcp = _make_mocks(entries, metadata)
        with _patch_modules(mock_checker, mock_audit, mock_mcp):
            await collect_ground_truth_automatically(dry_run=False)
        call_args = mock_checker.update_ground_truth.call_args
        assert call_args.kwargs["actual_correct"] is True
