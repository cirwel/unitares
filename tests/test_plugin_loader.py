"""Regression tests for src/plugin_loader.py.

load_plugins discovers governance_mcp.plugins entry points and calls each
register(). Two contracts matter and were untested:

  * the UNITARES_DISABLE_PLUGINS escape hatch must short-circuit to [] (used
    for test isolation and stripped OSS builds), and
  * failure isolation — "one broken plugin can't take governance down": a
    register() that raises is logged and skipped, never propagated, and the
    other plugins still load.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.plugin_loader as plugin_loader


def _ep(name, register_fn):
    return SimpleNamespace(name=name, value=f"pkg.{name}:register",
                           load=lambda: register_fn)


@pytest.fixture(autouse=True)
def _clear_disable_flag(monkeypatch):
    monkeypatch.delenv("UNITARES_DISABLE_PLUGINS", raising=False)


def test_disable_flag_short_circuits(monkeypatch):
    monkeypatch.setenv("UNITARES_DISABLE_PLUGINS", "1")
    called = []
    monkeypatch.setattr(plugin_loader, "entry_points",
                        lambda group: [_ep("p", lambda: called.append("p"))])
    assert plugin_loader.load_plugins() == []
    assert called == []   # never even enumerated


def test_loads_all_registered_plugins(monkeypatch):
    calls = []
    eps = [
        _ep("alpha", lambda: calls.append("alpha")),
        _ep("beta", lambda: calls.append("beta")),
    ]
    monkeypatch.setattr(plugin_loader, "entry_points", lambda group: eps)
    loaded = plugin_loader.load_plugins()
    assert sorted(loaded) == ["alpha", "beta"]
    assert sorted(calls) == ["alpha", "beta"]


def test_failure_is_isolated(monkeypatch):
    def boom():
        raise RuntimeError("plugin exploded")

    calls = []
    eps = [
        _ep("good_a", lambda: calls.append("good_a")),
        _ep("broken", boom),
        _ep("good_b", lambda: calls.append("good_b")),
    ]
    monkeypatch.setattr(plugin_loader, "entry_points", lambda group: eps)

    # Must not raise despite the broken plugin...
    loaded = plugin_loader.load_plugins()
    # ...and the broken one is excluded while the others still load.
    assert "broken" not in loaded
    assert sorted(loaded) == ["good_a", "good_b"]
    assert sorted(calls) == ["good_a", "good_b"]
