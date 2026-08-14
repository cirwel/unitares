"""
Tests for src/config_manager.py - Unified configuration manager.

Tests ConfigSource, ConfigManager, and convenience functions.
"""

import pytest
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import src.config_manager as config_manager
import src.runtime_config as runtime_config
from src.config_manager import (
    ConfigSource,
    ConfigManager,
    get_config_manager,
)


def test_get_thresholds_does_not_recurse():
    """Regression: the module wrapper used to shadow the import it delegates to.

    `config_manager` imports `get_thresholds` from `runtime_config` and also
    defines a module-level `get_thresholds()` convenience wrapper. The wrapper
    won the name, so `ConfigManager.get_thresholds()` called the wrapper, which
    called `get_config_manager().get_thresholds()`, and the pair recursed until
    the stack ended — taking `get_all_config` with it.

    An autouse fixture used to patch that away, which kept this suite green over
    a capability that could not run. The import is aliased now; this asserts the
    real code path instead of replacing it.
    """
    assert config_manager.get_thresholds() == runtime_config.get_thresholds()
    assert ConfigManager().get_thresholds() == runtime_config.get_thresholds()


# ============================================================================
# ConfigSource
# ============================================================================

class TestConfigSource:

    def test_creation(self):
        cs = ConfigSource(value=0.5, source="runtime", changeable=True)
        assert cs.value == 0.5
        assert cs.source == "runtime"
        assert cs.changeable is True
        assert cs.description is None

    def test_with_description(self):
        cs = ConfigSource(value=42, source="static", changeable=False, description="test")
        assert cs.description == "test"


# ============================================================================
# ConfigManager
# ============================================================================

class TestConfigManagerInit:

    def test_creates_successfully(self):
        cm = ConfigManager()
        assert cm._static_config is not None
        assert cm._core_params is not None


class TestGetThresholds:

    def test_returns_dict(self):
        cm = ConfigManager()
        thresholds = cm.get_thresholds()
        assert isinstance(thresholds, dict)

    def test_contains_expected_keys(self):
        cm = ConfigManager()
        thresholds = cm.get_thresholds()
        # Should have at least some standard threshold keys
        assert len(thresholds) > 0


class TestGetThreshold:

    def test_returns_float(self):
        cm = ConfigManager()
        val = cm.get_threshold("risk_approve_threshold", default=0.5)
        assert isinstance(val, (int, float))

    def test_default_used_for_unknown(self):
        cm = ConfigManager()
        val = cm.get_threshold("nonexistent_threshold_xyz", default=0.42)
        assert val == 0.42


class TestGetStaticConfig:

    def test_returns_config_instance(self):
        cm = ConfigManager()
        static = cm.get_static_config()
        assert static is not None
        assert hasattr(static, "RISK_APPROVE_THRESHOLD")
        assert hasattr(static, "RISK_REVISE_THRESHOLD")


class TestGetCoreParams:

    def test_returns_dynamics_params(self):
        cm = ConfigManager()
        params = cm.get_core_params()
        assert hasattr(params, "alpha")
        assert hasattr(params, "mu")


class TestGetServerConstants:

    def test_returns_dict(self):
        cm = ConfigManager()
        constants = cm.get_server_constants()
        assert isinstance(constants, dict)
        assert "MAX_KEEP_PROCESSES" in constants
        assert "SERVER_VERSION" in constants


class TestGetAllConfig:

    def test_returns_dict(self):
        cm = ConfigManager()
        all_config = cm.get_all_config()
        assert isinstance(all_config, dict)
        assert len(all_config) > 0

    def test_values_are_config_source(self):
        cm = ConfigManager()
        all_config = cm.get_all_config()
        for key, val in all_config.items():
            assert isinstance(val, ConfigSource), f"Key {key} is not ConfigSource"

    def test_has_threshold_entries(self):
        cm = ConfigManager()
        all_config = cm.get_all_config()
        threshold_keys = [k for k in all_config if k.startswith("threshold.")]
        assert len(threshold_keys) > 0

    def test_has_static_entries(self):
        cm = ConfigManager()
        all_config = cm.get_all_config()
        static_keys = [k for k in all_config if k.startswith("static.")]
        assert len(static_keys) > 0

    def test_has_core_entries(self):
        cm = ConfigManager()
        all_config = cm.get_all_config()
        core_keys = [k for k in all_config if k.startswith("core.")]
        assert len(core_keys) > 0

    def test_has_server_entries(self):
        cm = ConfigManager()
        all_config = cm.get_all_config()
        server_keys = [k for k in all_config if k.startswith("server.")]
        assert len(server_keys) > 0

    def test_threshold_entries_are_changeable(self):
        cm = ConfigManager()
        all_config = cm.get_all_config()
        for key, val in all_config.items():
            if key.startswith("threshold."):
                assert val.changeable is True
                assert val.source == "runtime"

    def test_static_entries_are_not_changeable(self):
        cm = ConfigManager()
        all_config = cm.get_all_config()
        for key, val in all_config.items():
            if key.startswith("static."):
                assert val.changeable is False
                assert val.source == "static"


class TestGetConfigInfo:

    def test_returns_dict(self):
        cm = ConfigManager()
        info = cm.get_config_info()
        assert isinstance(info, dict)

    def test_has_expected_categories(self):
        cm = ConfigManager()
        info = cm.get_config_info()
        assert "runtime_changeable" in info
        assert "static" in info
        assert "core" in info
        assert "server" in info

    def test_categories_have_description(self):
        cm = ConfigManager()
        info = cm.get_config_info()
        for category, data in info.items():
            assert "description" in data
            assert "configs" in data


# ============================================================================
# get_config_manager singleton
# ============================================================================

class TestGetConfigManager:

    def test_returns_instance(self):
        cm = get_config_manager()
        assert isinstance(cm, ConfigManager)

    def test_returns_same_instance(self):
        cm1 = get_config_manager()
        cm2 = get_config_manager()
        assert cm1 is cm2
