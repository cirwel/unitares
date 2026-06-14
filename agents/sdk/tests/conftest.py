"""Pytest configuration for the unitares-sdk test suite.

The resident roster is deployment config (UNITARES_RESIDENTS), empty by
default. The SDK's KNOWN_RESIDENT_NAMES is resolved at module-import time
from this env var, so configure the canonical fleet here BEFORE any test
imports unitares_sdk._substrate.
"""
import os

os.environ.setdefault(
    "UNITARES_RESIDENTS",
    "Lumen,Vigil,Sentinel,Watcher,Steward,Chronicler",
)
