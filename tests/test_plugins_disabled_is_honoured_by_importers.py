"""``UNITARES_DISABLE_PLUGINS`` must fence every path into a plugin package.

``src/plugin_loader.py`` promises the flag "skips plugin loading entirely",
but until 2026-09-09 it only gated the entry-point loader. Importing a plugin
module directly runs its ``@mcp_tool`` decorators, which register tools into
the registry the interface contract is built from. Before the progressive
gateway, doing that after ``mcp_server_bootstrap`` mounted the surface made the
tool COUNTED and never DISPATCHABLE.

Measured on a server started with the flag set, polling once a second:

    t=0.1s  advertised=50  contract_count=50
    t=5.5s  advertised=50  contract_count=51   <- pi_restart_service

and calling that 51st capability returned "Unknown tool". The five seconds are
``deep_health_probe_task``'s startup sleep; the probe calls
``get_health_check_data``, which imported ``unitares_pi_plugin.handlers``
behind an ``ImportError`` guard that says nothing about the flag.

This matters beyond tests: the flag is documented (docs/FLAGS.md) and exists
for stripped OSS builds, so publishing an undispatchable federation capability
was a reachable production state, not a test artifact.

That importer, the deep-health Pi probe, was removed with the rest of the
Mac-to-Pi coupling on 2026-09-13, so no shipped module imports a plugin package
by name any more. Progressive advertisement now makes late visible handlers
reachable through ``use_tool``; what remains here holds for any plugin: the
predicate every future importer must share, and the invariant that the mount
exposes the gateway while the contract names only visible dispatch handlers.
"""

from __future__ import annotations

import os

import pytest

from src.plugin_loader import plugins_disabled


class TestPredicate:
    def test_absent_means_plugins_allowed(self, monkeypatch):
        monkeypatch.delenv("UNITARES_DISABLE_PLUGINS", raising=False)
        assert plugins_disabled() is False

    def test_empty_string_means_allowed(self, monkeypatch):
        """Matches load_plugins()'s own truthiness test, which the loader has
        always used; an empty value has never disabled anything."""
        monkeypatch.setenv("UNITARES_DISABLE_PLUGINS", "")
        assert plugins_disabled() is False

    @pytest.mark.parametrize("value", ["1", "0", "true", "no", "anything"])
    def test_any_non_empty_value_disables(self, monkeypatch, value):
        """⛔Including "0" and "no". The loader tests truthiness of the string,
        not its meaning, and this predicate must not diverge from it — two
        readers of one flag disagreeing is how the original defect hid.
        """
        monkeypatch.setenv("UNITARES_DISABLE_PLUGINS", value)
        assert plugins_disabled() is True

    def test_the_loader_and_the_predicate_read_the_same_flag(self, monkeypatch):
        from src import plugin_loader

        monkeypatch.setenv("UNITARES_DISABLE_PLUGINS", "1")
        assert plugin_loader.load_plugins() == []
        assert plugins_disabled() is True


class TestContractCannotOutrunDispatch:
    """The invariant nobody had written down.

    The progressive mount may omit a capability schema only when it mounts the
    discovery/detail/gateway trio that reaches the visible live registry. The
    federation contract may therefore be wider than ``tools/list``, but must
    never include hidden handlers or names dispatch cannot resolve.

    ⛔This is NOT the plugin flag's problem, which is why fixing callers could
    not close it. The same divergence is reachable with plugins ENABLED
    whenever the package is importable but its entry point was not discovered
    (missing metadata, stale egg-info, a ``register()`` that raised and was
    swallowed at src/plugin_loader.py) — because bootstrap remounts only when a
    plugin actually loaded. Verified 2026-09-09 by importing the plugin module
    after building the contract: 50 capabilities became 51 with the flag unset.
    """

    def test_progressive_gateway_keeps_live_contract_dispatchable(self):
        """Late visible handlers remain reachable without remounting schemas."""
        import sys
        import types

        from src.interface_contract import build_interface_contract
        from src.mcp_handlers.decorators import get_tool_registry, is_tool_hidden
        from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES

        # Stand in for a mounted server: the real one is a subprocess, and this
        # invariant is about the reconciliation, not about the transport.
        mounted = {name: object() for name in (
            "start_session", "sync_state", "health_check", "identity",
            "list_tools", "describe_tool", "use_tool",
        )}
        fake_server = types.ModuleType("src.mcp_server")
        fake_server.mcp = types.SimpleNamespace(
            _tool_manager=types.SimpleNamespace(_tools=mounted)
        )
        original = sys.modules.get("src.mcp_server")
        sys.modules["src.mcp_server"] = fake_server
        try:
            capabilities = build_interface_contract("full")["capabilities"]
        finally:
            if original is None:
                sys.modules.pop("src.mcp_server", None)
            else:
                sys.modules["src.mcp_server"] = original

        advertised = {c["name"] for c in capabilities}
        visible_dispatch = {
            name for name in get_tool_registry()
            if not is_tool_hidden(name)
        } | set(AGENT_WORKFLOW_ALIASES)
        gateway = {"list_tools", "describe_tool", "use_tool"}

        assert gateway <= set(mounted)
        assert advertised <= visible_dispatch
        assert advertised - set(mounted), (
            "the fixture must exercise capabilities reached through use_tool, "
            "not only names mounted directly"
        )

    def test_an_unmounted_server_still_describes_the_registry(self):
        """Generators and unit tests must keep their pre-existing behaviour.

        ``mounted_tool_names()`` returns None rather than an empty set when no
        server module is imported, so ``flag_catalog.py``, ``tool_edge_index.py``
        and ``python -m src.interface_contract`` describe the repo exactly as
        they did before. An empty set here would silently empty the contract.
        """
        import sys

        from src.interface_contract import build_interface_contract, mounted_tool_names

        original = sys.modules.pop("src.mcp_server", None)
        try:
            assert mounted_tool_names() is None
            capabilities = build_interface_contract("full")["capabilities"]
        finally:
            if original is not None:
                sys.modules["src.mcp_server"] = original

        assert len(capabilities) > 40, (
            "with no server mounted the contract must still describe the registry"
        )
