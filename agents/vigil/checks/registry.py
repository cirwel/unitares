"""In-process registry for Vigil checks.

Built-in checks live in this package and register on first load_plugins().
External checks are imported from modules listed in VIGIL_CHECK_PLUGINS
(colon-separated). Each plugin module is expected to call register() at
import time.
"""

from __future__ import annotations

import importlib
import os
from typing import List

from .base import Check

_CHECKS: List[Check] = []
_LOADED: bool = False


def register(check: Check) -> None:
    _CHECKS.append(check)


def all_checks() -> List[Check]:
    return list(_CHECKS)


def load_plugins() -> None:
    """Import built-in and external check modules so they self-register.

    Idempotent: subsequent calls are no-ops.
    External plugins are resolved from VIGIL_CHECK_PLUGINS (colon-separated
    module paths). A missing module raises ImportError — typos should be loud.
    """
    global _LOADED
    if _LOADED:
        return

    # Built-ins: register explicitly rather than via import side-effects, so
    # Python's module cache doesn't swallow registration on re-load (test harness).
    from .governance_health import GovernanceHealth
    register(GovernanceHealth())
    from .resident_tag_hygiene import ResidentTagHygiene
    register(ResidentTagHygiene())
    from .plugin_hook_liveness import PluginHookLiveness
    register(PluginHookLiveness())

    raw = os.getenv("VIGIL_CHECK_PLUGINS", "") or ""
    for mod_path in raw.split(":"):
        mod_path = mod_path.strip()
        if mod_path:
            importlib.import_module(mod_path)

    _LOADED = True
