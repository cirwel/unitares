"""
Wave 3a env-flag startup-hook tests (PR #5).

Specification:
    ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §3.1 and §5
    PR #5. ``src/wave3a_routing.py::apply_env_flag_routes`` reads
    ``WAVE_3A_HEALTH_CHECK_ON_BEAM`` (and the analogous flags added by PR
    #6/#7/#8) at MCP startup; truthy → set a routing-table row; falsy/unset
    → no-op.

Hard invariants (§3.1):

  * Process restart with the flag UNSET yields an empty routing table.
  * Process restart with the flag SET yields a single row pointing at the
    BEAM URL for the corresponding handler.
  * The hook is idempotent — calling it twice does not duplicate or
    invalidate rows.

These tests exercise the env-flag table directly. The MCP startup wiring
in ``src/mcp_server.py`` calls ``apply_env_flag_routes()`` once at boot;
if the hook itself is correct, the boot-time invocation is correct by
construction.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src import wave3a_routing  # noqa: E402


@pytest.fixture(autouse=True)
def clean_routing_table():
    """Empty the routing table before and after every test.

    §3.1 hard invariant: process start = empty table. The test process
    persists for the whole pytest session, so we clear the table around
    each test to simulate the boot-empty posture.
    """
    wave3a_routing.clear_routes()
    yield
    wave3a_routing.clear_routes()


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every WAVE_3A_*_ON_BEAM flag from the env for the test.

    Avoids cross-pollution from the operator's shell or a leftover flag in
    a CI runner.
    """
    for env_var in list(os.environ):
        if env_var.startswith("WAVE_3A_") and env_var.endswith("_ON_BEAM"):
            monkeypatch.delenv(env_var, raising=False)


class TestApplyEnvFlagRoutes:
    """Exercise the env-flag startup hook directly."""

    def test_unset_flag_leaves_routing_table_empty(self, clean_env):
        added = wave3a_routing.apply_env_flag_routes()
        assert added == []
        assert wave3a_routing.list_routes() == {}

    def test_empty_string_flag_is_falsy(self, monkeypatch, clean_env):
        monkeypatch.setenv("WAVE_3A_HEALTH_CHECK_ON_BEAM", "")
        added = wave3a_routing.apply_env_flag_routes()
        assert added == []
        assert wave3a_routing.list_routes() == {}

    def test_false_value_is_falsy(self, monkeypatch, clean_env):
        for value in ("false", "False", "FALSE", "0", "no", "off"):
            monkeypatch.setenv("WAVE_3A_HEALTH_CHECK_ON_BEAM", value)
            wave3a_routing.clear_routes()
            added = wave3a_routing.apply_env_flag_routes()
            assert added == [], (
                f"flag value {value!r} should be falsy; got added={added}"
            )

    def test_true_value_adds_health_check_route(self, monkeypatch, clean_env):
        monkeypatch.setenv("WAVE_3A_HEALTH_CHECK_ON_BEAM", "true")
        added = wave3a_routing.apply_env_flag_routes()
        assert added == ["health_check"]

        routes = wave3a_routing.list_routes()
        assert "health_check" in routes
        assert routes["health_check"] == (
            "http://127.0.0.1:8770/v1/handlers/health_check"
        )

    def test_alternative_truthy_values(self, monkeypatch, clean_env):
        for value in ("true", "True", "TRUE", "1", "yes", "Y", "on"):
            monkeypatch.setenv("WAVE_3A_HEALTH_CHECK_ON_BEAM", value)
            wave3a_routing.clear_routes()
            added = wave3a_routing.apply_env_flag_routes()
            assert added == ["health_check"], (
                f"flag value {value!r} should be truthy; got added={added}"
            )

    def test_idempotent_under_repeated_invocation(self, monkeypatch, clean_env):
        # §3.1 invariant: re-running the hook (e.g., from a launchd
        # one-shot) does not duplicate or invalidate rows.
        monkeypatch.setenv("WAVE_3A_HEALTH_CHECK_ON_BEAM", "true")

        added_a = wave3a_routing.apply_env_flag_routes()
        routes_a = wave3a_routing.list_routes()

        added_b = wave3a_routing.apply_env_flag_routes()
        routes_b = wave3a_routing.list_routes()

        assert added_a == added_b == ["health_check"]
        assert routes_a == routes_b
        assert wave3a_routing.route_count() == 1

    def test_default_off_posture(self, clean_env):
        """The MOST IMPORTANT property of PR #5: default-OFF.

        With no env vars set, the startup hook MUST NOT add any routes.
        This is the behavioral guarantee that PR #5 lands a no-op in
        master and only flips behavior under explicit operator action.
        """
        # Sanity: the env-flag table has at least one entry (otherwise
        # the test is vacuous).
        assert wave3a_routing._ENV_FLAG_ROUTES, (
            "_ENV_FLAG_ROUTES is empty — this test depends on at least "
            "one entry being present"
        )

        added = wave3a_routing.apply_env_flag_routes()
        assert added == [], (
            "default-OFF violated: env-flag hook added routes "
            f"without operator opt-in: {added}"
        )
        assert wave3a_routing.route_count() == 0
