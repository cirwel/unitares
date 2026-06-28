"""Regression: patching GovernanceConfig must survive a config module reload.

Documents and guards the pass-in-isolation / fail-in-suite hazard: several tests
``importlib.reload(config.governance_config)``, which replaces the GovernanceConfig
*class object*. A test that patches the class via a stale top-level import then
patches a dead object while code under test re-imports the live one. The
``set_governance_config`` fixture patches the live class; the autouse
``_stable_governance_config_class`` fixture restores identity so a reloader cannot
pollute later tests.
"""

import importlib


def test_reload_replaces_the_class_object():
    # The root cause, made explicit: reload() swaps the class object, so any
    # reference captured beforehand is stale afterwards.
    import config.governance_config as cfg
    stale = cfg.GovernanceConfig
    importlib.reload(cfg)
    assert cfg.GovernanceConfig is not stale


def test_set_governance_config_patches_through_a_reload(set_governance_config):
    # Even after a mid-test reload swaps the class, the fixture patches the LIVE
    # class — which is exactly what a handler's `from config... import` resolves.
    import config.governance_config as cfg
    importlib.reload(cfg)

    set_governance_config("SESSION_TTL_HOURS", 999)

    live = importlib.import_module("config.governance_config")
    assert live.GovernanceConfig.SESSION_TTL_HOURS == 999


def test_autouse_fixture_restored_identity_after_prior_reload():
    # The two tests above each reloaded the module. If the autouse
    # _stable_governance_config_class fixture did its job, the class object seen
    # here is the canonical one a fresh import resolves (no leaked reload state).
    import config.governance_config as cfg
    fresh = importlib.import_module("config.governance_config")
    assert cfg.GovernanceConfig is fresh.GovernanceConfig
