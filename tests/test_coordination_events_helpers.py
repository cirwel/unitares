"""Contract tests for `governance_core/coordination_events_helpers.py`.

Pinning the payload-shape rules so emission-site bugs surface here, not
in production audit gaps.
"""

from __future__ import annotations

import pytest

from governance_core.coordination_events_helpers import (
    VALID_ERROR_CLASSES,
    make_boundary_payload,
)


class TestValidPayloads:
    def test_timeout_with_no_status_code(self):
        payload = make_boundary_payload(
            endpoint="https://beam.local/v1/dispatch",
            method="POST",
            error_class="timeout",
            status_code=None,
            elapsed_ms=2050,
        )
        assert payload == {
            "endpoint": "https://beam.local/v1/dispatch",
            "method": "POST",
            "error_class": "timeout",
            "status_code": None,
            "elapsed_ms": 2050,
        }

    def test_non_200_requires_status_code(self):
        payload = make_boundary_payload(
            endpoint="governance_mcp/process_agent_update",
            method="POST",
            error_class="non_200",
            status_code=502,
            elapsed_ms=140,
        )
        assert payload["status_code"] == 502
        assert payload["error_class"] == "non_200"

    def test_connect_error_with_unmeasured_elapsed(self):
        payload = make_boundary_payload(
            endpoint="https://beam.local/v1/identity/resolve",
            method="GET",
            error_class="connect_error",
            status_code=None,
            elapsed_ms=None,
        )
        assert payload["elapsed_ms"] is None
        assert payload["status_code"] is None

    def test_payload_is_a_fresh_dict(self):
        # Ensure callers can't shoot themselves by mutating a shared default.
        a = make_boundary_payload(
            endpoint="x", method="POST", error_class="other",
            status_code=None, elapsed_ms=None,
        )
        b = make_boundary_payload(
            endpoint="x", method="POST", error_class="other",
            status_code=None, elapsed_ms=None,
        )
        a["endpoint"] = "mutated"
        assert b["endpoint"] == "x"

    def test_key_order_matches_contract(self):
        payload = make_boundary_payload(
            endpoint="x", method="GET", error_class="decode_error",
            status_code=None, elapsed_ms=42,
        )
        assert list(payload.keys()) == ["endpoint", "method", "error_class", "status_code", "elapsed_ms"]


class TestRejectsInvalidInput:
    def test_empty_endpoint_rejected(self):
        with pytest.raises(ValueError, match="endpoint"):
            make_boundary_payload(
                endpoint="", method="POST", error_class="other",
                status_code=None, elapsed_ms=None,
            )

    def test_whitespace_endpoint_rejected(self):
        with pytest.raises(ValueError, match="endpoint"):
            make_boundary_payload(
                endpoint="   ", method="POST", error_class="other",
                status_code=None, elapsed_ms=None,
            )

    def test_empty_method_rejected(self):
        with pytest.raises(ValueError, match="method"):
            make_boundary_payload(
                endpoint="x", method="", error_class="other",
                status_code=None, elapsed_ms=None,
            )

    def test_unknown_error_class_rejected(self):
        with pytest.raises(ValueError, match="error_class"):
            make_boundary_payload(
                endpoint="x", method="POST", error_class="weird",
                status_code=None, elapsed_ms=None,
            )

    def test_none_error_class_rejected(self):
        with pytest.raises(ValueError, match="error_class"):
            make_boundary_payload(
                endpoint="x", method="POST", error_class=None,  # type: ignore[arg-type]
                status_code=None, elapsed_ms=None,
            )

    def test_non_200_without_status_code_rejected(self):
        with pytest.raises(ValueError, match="status_code"):
            make_boundary_payload(
                endpoint="x", method="POST", error_class="non_200",
                status_code=None, elapsed_ms=None,
            )

    def test_status_code_wrong_type(self):
        with pytest.raises(TypeError, match="status_code"):
            make_boundary_payload(
                endpoint="x", method="POST", error_class="non_200",
                status_code="500",  # type: ignore[arg-type]
                elapsed_ms=None,
            )

    def test_elapsed_ms_wrong_type(self):
        with pytest.raises(TypeError, match="elapsed_ms"):
            make_boundary_payload(
                endpoint="x", method="POST", error_class="other",
                status_code=None,
                elapsed_ms="50",  # type: ignore[arg-type]
            )


class TestEnumIntegrity:
    def test_valid_error_classes_match_documented_set(self):
        # Pinned against `src/coordination_events.py`'s payload-shape comment.
        # Adding a new error_class requires updating BOTH this set AND the
        # documentation comment in coordination_events.py.
        assert VALID_ERROR_CLASSES == frozenset({
            "timeout",
            "connect_error",
            "non_200",
            "decode_error",
            "other",
        })


class TestMakeShadowDivergencePayload:
    """Wave 3 §8.2 shadow-divergence payload contract (prereq PR #1)."""

    def _make(self, **overrides):
        from governance_core.coordination_events_helpers import (
            make_shadow_divergence_payload,
        )

        kwargs = dict(
            table_name="identities",
            agent_id="ag-123",
            kind="column_mismatch",
            divergent_columns=["status", "metadata"],
        )
        kwargs.update(overrides)
        return make_shadow_divergence_payload(**kwargs)

    def test_column_mismatch_happy_path_and_key_order(self):
        payload = self._make()
        assert payload == {
            "table_name": "identities",
            "agent_id": "ag-123",
            "kind": "column_mismatch",
            "divergent_columns": ["status", "metadata"],
        }
        assert list(payload.keys()) == [
            "table_name", "agent_id", "kind", "divergent_columns",
        ]

    def test_missing_row_kinds_require_empty_columns(self):
        payload = self._make(kind="shadow_missing", divergent_columns=[])
        assert payload["divergent_columns"] == []
        payload = self._make(kind="canonical_missing", divergent_columns=[])
        assert payload["kind"] == "canonical_missing"

    def test_column_mismatch_requires_columns(self):
        with pytest.raises(ValueError, match="divergent column"):
            self._make(divergent_columns=[])

    def test_missing_kind_rejects_columns(self):
        with pytest.raises(ValueError, match="wholesale"):
            self._make(kind="shadow_missing", divergent_columns=["status"])

    def test_unknown_table_rejected(self):
        with pytest.raises(ValueError, match="table_name"):
            self._make(table_name="dialectic_sessions")

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="kind"):
            self._make(kind="drifted")

    def test_empty_agent_id_rejected(self):
        with pytest.raises(ValueError, match="agent_id"):
            self._make(agent_id="  ")

    def test_non_list_columns_rejected(self):
        with pytest.raises(TypeError, match="divergent_columns"):
            self._make(divergent_columns="status")

    def test_returns_fresh_list(self):
        cols = ["status"]
        payload = self._make(divergent_columns=cols)
        cols.append("mutated")
        assert payload["divergent_columns"] == ["status"]
