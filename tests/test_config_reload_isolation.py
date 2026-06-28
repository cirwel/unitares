"""Regression + docs for the config.governance_config reload hazard.

Several suite tests ``importlib.reload(config.governance_config)``, which replaces
BOTH the ``GovernanceConfig`` class object AND the module-level ``config`` singleton
(`config = GovernanceConfig()`). Modules that did
``from config.governance_config import GovernanceConfig`` / ``import … config`` keep
*stale* references, so a test that patches via a stale ref patches a dead object
while code under test reads the live one — pass-in-isolation / fail-in-suite.

The ``set_governance_config`` fixture (in conftest) patches the LIVE class to avoid
that. These tests are deliberately **hermetic**: every reload is fully restored
(class + singleton) so this file cannot leak the very pollution it documents into a
later test such as test_governance_monitor's TestHistoryTrimming.
"""

import importlib

import pytest


@pytest.fixture
def reloadable_config():
    """Yield the config module; restore class + singleton after any reload.

    Restoring both is what keeps governance_monitor's ``from … import config``
    reference consistent — restoring only the class (as an earlier draft did) still
    leaks the swapped singleton and breaks HISTORY_WINDOW trimming downstream.
    """
    import config.governance_config as cfg
    saved_cls, saved_inst = cfg.GovernanceConfig, cfg.config
    yield cfg
    cfg.GovernanceConfig = saved_cls
    cfg.config = saved_inst


def test_reload_replaces_class_and_singleton(reloadable_config):
    cfg = reloadable_config
    stale_cls, stale_inst = cfg.GovernanceConfig, cfg.config
    importlib.reload(cfg)
    # The root cause, made explicit: reload swaps BOTH references.
    assert cfg.GovernanceConfig is not stale_cls
    assert cfg.config is not stale_inst


def test_set_governance_config_is_reload_safe(reloadable_config, set_governance_config):
    # Even after a reload swaps the class, the helper patches the LIVE class —
    # exactly what a handler's `from config… import GovernanceConfig` resolves.
    importlib.reload(reloadable_config)
    set_governance_config("SESSION_TTL_HOURS", 4242)
    live = importlib.import_module("config.governance_config")
    assert live.GovernanceConfig.SESSION_TTL_HOURS == 4242


def test_set_governance_config_no_reload(set_governance_config):
    # The common case: no reload in play, the helper just patches the class.
    import config.governance_config as cfg
    set_governance_config("SESSION_TTL_HOURS", 7)
    assert cfg.GovernanceConfig.SESSION_TTL_HOURS == 7
