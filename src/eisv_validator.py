"""Compatibility alias for :mod:`src.eisv.validation`.

The legacy module name remains importable and resolves to the canonical module
object so ``VALIDATION_ENABLED`` and exported identities stay shared.
"""

import importlib
from pathlib import Path
import runpy
import sys


_CANONICAL_MODULE = "src.eisv.validation"

if __name__ == "__main__":
    # Direct execution historically worked for this dependency-free example.
    # Make the package root importable in that mode as well as under ``-m``.
    if not __package__:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    runpy.run_module(_CANONICAL_MODULE, run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module(_CANONICAL_MODULE)
