"""``UNITARES_DISABLE_PLUGINS`` must fence every path into a plugin package.

``src/plugin_loader.py`` promises the flag "skips plugin loading entirely",
but until 2026-09-09 it only gated the entry-point loader. Importing a plugin
module directly runs its ``@mcp_tool`` decorators, which register tools into
the registry the interface contract is built from. That happens after
``mcp_server_bootstrap`` has mounted the surface, so the tool is COUNTED and
never DISPATCHABLE.

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


class TestHealthProbeIsFenced:
    """The specific path that produced the undispatchable capability.

    Both tests below are written so they cannot pass vacuously. An earlier
    draft swallowed every exception from the health payload and then accepted
    an empty import log — so a failure in any unrelated check before the Pi
    block would have produced a green test that never reached the guard.
    """

    @pytest.mark.asyncio
    async def test_health_check_consults_the_flag_and_skips_the_import(
        self, monkeypatch
    ):
        """The guard must be REACHED and must block, not merely appear to.

        Reaching is proved by spying on the predicate: if the Pi block is never
        entered, the spy records nothing and the test fails. Blocking is proved
        by the import watcher. An ``ImportError`` guard alone cannot satisfy
        this, because the plugin is importable on the machine where the defect
        appeared.
        """
        import builtins

        import src.mcp_handlers  # noqa: F401 — settle the import cycle first
        from src.services import runtime_queries

        monkeypatch.setenv("UNITARES_DISABLE_PLUGINS", "1")

        consulted: list[bool] = []

        def spy() -> bool:
            result = bool(os.environ.get("UNITARES_DISABLE_PLUGINS"))
            consulted.append(result)
            return result

        monkeypatch.setattr("src.plugin_loader.plugins_disabled", spy)

        attempted: list[str] = []
        real_import = builtins.__import__

        def watching_import(name, *args, **kwargs):
            if name.startswith("unitares_pi_plugin"):
                attempted.append(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", watching_import)
        await runtime_queries.get_health_check_data({"lite": True})

        assert consulted == [True], (
            "the Pi block did not consult plugins_disabled(); the guard was "
            f"not reached (calls recorded: {consulted})"
        )
        assert attempted == [], (
            f"plugin import attempted with UNITARES_DISABLE_PLUGINS set: {attempted}"
        )

    @pytest.mark.asyncio
    async def test_the_pi_check_still_runs_when_plugins_are_enabled(
        self, monkeypatch
    ):
        """The fence must not become a permanent skip.

        Uses a stand-in plugin module rather than the real one, so this runs on
        CI, where no ``governance_mcp.plugins`` package is installed — which is
        the only environment that gates merges. Asserts the probe was actually
        CALLED and its result reached the payload, not merely that an import
        was attempted: an import that then failed would satisfy the weaker
        check while the connectivity signal stayed silently dead.
        """
        import sys
        import types

        import src.mcp_handlers  # noqa: F401
        from src.services import runtime_queries

        monkeypatch.delenv("UNITARES_DISABLE_PLUGINS", raising=False)

        called: list[tuple] = []

        async def fake_call_pi_tool(tool, args, timeout=None):
            called.append((tool, args))
            return {"ok": True}

        fake_pkg = types.ModuleType("unitares_pi_plugin")
        fake_handlers = types.ModuleType("unitares_pi_plugin.handlers")
        fake_handlers.PI_MCP_URLS = {"pi": "http://127.0.0.1:0/mcp/"}
        fake_handlers.call_pi_tool = fake_call_pi_tool
        fake_pkg.handlers = fake_handlers
        monkeypatch.setitem(sys.modules, "unitares_pi_plugin", fake_pkg)
        monkeypatch.setitem(sys.modules, "unitares_pi_plugin.handlers", fake_handlers)

        result = await runtime_queries.get_health_check_data({"lite": True})

        assert called, "the Pi connectivity probe never ran with plugins enabled"
        checks = result.get("checks", result)
        assert "pi_connectivity" in checks, (
            f"the probe ran but its result never reached the payload: {sorted(checks)}"
        )


class TestContractCannotOutrunDispatch:
    """The invariant nobody had written down.

    A capability may appear in the federation contract only if the mounted
    surface will dispatch it. ``tools/list`` serves FastMCP's mounted table
    while ``src/interface_contract.py`` builds from the decorator registry, and
    the two are reconciled exactly once, at
    ``mcp_server_bootstrap.auto_register_all_tools``. Every late registration
    after that point is an advertised promise the server breaks.

    ⛔This is NOT the plugin flag's problem, which is why fixing callers could
    not close it. The same divergence is reachable with plugins ENABLED
    whenever the package is importable but its entry point was not discovered
    (missing metadata, stale egg-info, a ``register()`` that raised and was
    swallowed at src/plugin_loader.py) — because bootstrap remounts only when a
    plugin actually loaded. Verified 2026-09-09 by importing the plugin module
    after building the contract: 50 capabilities became 51 with the flag unset.
    """

    def test_no_capability_is_advertised_that_dispatch_would_refuse(self):
        """Fails on a live server whose registry grew after the mount."""
        import sys
        import types

        from src.interface_contract import build_interface_contract

        # Stand in for a mounted server: the real one is a subprocess, and this
        # invariant is about the reconciliation, not about the transport.
        mounted = {name: object() for name in (
            "start_session", "sync_state", "health_check", "identity",
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
        assert advertised <= set(mounted), (
            "contract advertises capabilities the mounted surface will refuse: "
            f"{sorted(advertised - set(mounted))}"
        )
        assert advertised, "narrowing removed everything; the intersection is wrong"

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
